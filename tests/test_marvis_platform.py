"""腾讯 Marvis 平台：协议层回归测试。

这些断言锁的都是「写错了不会报错、只会静默出问题」的地方 ——

  * 签名原文拼错 → 上游 403，报错只说「签名错误」，看不出少了个字段；
  * `Ual-Access-MarvisExt` 带空格 → 服务端按原文比对，静默判无效；
  * 模型 id 不归一 → 400 无效模型 id，看着像「账号坏了」；
  * 不可用模型回 401 → 上层当成登录失效，把整池好号全标死；
  * QQ 的 state 写成整数 → 上游判参数无效（必须与渲染层一致是小数）。
"""
import base64
import hashlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

# 必须早于 `admin.*` 导入：`admin/db.py` 在模块级按 settings 建引擎，
# 默认值是 MySQL；固定成 SQLite 文件库才能脱库跑（内存库不接受连接池参数）。
os.environ.setdefault(
    "ADMIN_DATABASE_URL",
    "sqlite:///" + str(Path(os.environ.get("TEMP", "/tmp")) / "marvis_test.db"),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin.platform.marvis.core import client, endpoints  # noqa: E402
from admin.platform.marvis.core import uskey as uskey_sidecar  # noqa: E402
from admin.platform.marvis.services import accounts as marvis_accounts  # noqa: E402

ACCT = {"openid": "openid-abc", "access_token": "tok-xyz", "guid": "guid-123",
        "login_type": "1"}

#: 签名替身：单测不打真签名器（纯 Python mint 的可复现性由
#: `tests/test_marvis_uskey_local.py` 覆盖）。
#: 这里只锁「header / bus_info / MarvisExt 三者自洽」。
FAKE_QIMEI36 = "3aeda2c4343993fc64eec8c830001591ab11"


def _fake_uskey(nbytes: int = 376) -> str:
    return base64.b64encode(b"\x0a\xc0\xd5\x0c\x21\x11"
                            + bytes(nbytes - 6)).decode()


def _fake_signer(**_kwargs) -> dict:
    return {"uskey": _fake_uskey(), "qimei36": FAKE_QIMEI36, "bus_info": "",
            "uskey_bytes": 376, "uskey_len": 504, "ms": 0}


def setUpModule():
    uskey_sidecar.set_signer(_fake_signer)


def tearDownModule():
    uskey_sidecar.set_signer(None)


class SignatureTests(unittest.TestCase):
    def test_signature_is_md5_of_concatenation(self):
        """签名口径：md5(query + timestamp + accessKey + nonce)，小写 hex。"""
        expected = hashlib.md5(
            ("{}" + "1700000000000" + endpoints.ACCESS_KEY + "1234").encode()
        ).hexdigest()
        self.assertEqual(
            endpoints.ual_signature("{}", "1700000000000", "1234"), expected)
        self.assertEqual(len(expected), 32)
        self.assertEqual(expected, expected.lower())

    def test_business_headers_carry_signature_and_identity(self):
        headers = endpoints.business_headers(ACCT, query="{}", method="POST")
        for key in ("Ual-Access-Businessid", "Ual-Access-Timestamp",
                    "Ual-Access-Nonce", "Ual-Access-Signature",
                    "Ual-Access-Access-Token", "Ual-Access-Openid",
                    "Ual-Access-Guid", "Ual-Access-Login-Type"):
            self.assertIn(key, headers, "缺 %s" % key)
        # 签名必须能用同一组 timestamp/nonce 复算出来
        again = endpoints.ual_signature(
            "{}", headers["Ual-Access-Timestamp"], headers["Ual-Access-Nonce"])
        self.assertEqual(headers["Ual-Access-Signature"], again)
        self.assertEqual(headers["Ual-Access-Businessid"], endpoints.BUSINESS_ID)

    def test_business_headers_use_browser_ua(self):
        """业务接口必须用客户端浏览器 UA；换成普通 Chrome UA 会被判参数无效。"""
        headers = endpoints.business_headers(ACCT, query="{}")
        self.assertIn("Marvis/", headers["User-Agent"])


class LlmHeaderTests(unittest.TestCase):
    def test_llm_headers_shape(self):
        headers = endpoints.llm_headers(ACCT)
        for key in ("Ual-Access-Openid", "Ual-Access-Access-Token",
                    "Ual-Access-Login-Type", "Ual-Access-Guid",
                    "Ual-Access-MarvisExt", "Marvis-Protocol-Version"):
            self.assertIn(key, headers, "缺 %s" % key)
        self.assertEqual(headers["Ual-Access-Openid"], ACCT["openid"])
        self.assertEqual(headers["Ual-Access-Access-Token"], ACCT["access_token"])

    def test_llm_headers_do_not_carry_business_signature(self):
        """模型网关**不能**带签名头 —— 会把 403 风控变成 401 登录失效（实测）。"""
        headers = endpoints.llm_headers(ACCT)
        self.assertNotIn("Ual-Access-Signature", headers)
        self.assertNotIn("Ual-Access-Timestamp", headers)

    def test_llm_ua_is_the_asyncopenai_one(self):
        headers = endpoints.llm_headers(ACCT)
        self.assertEqual(headers["User-Agent"], endpoints.CLIENT_UA)
        self.assertIn("AsyncOpenAI", headers["User-Agent"])

    def test_marvis_ext_is_compact_json(self):
        """`Ual-Access-MarvisExt` 必须无空格 —— 服务端按原文字符串校验。"""
        ext = endpoints.marvis_ext(
            ACCT, uskey_value=_fake_uskey(), qimei36=FAKE_QIMEI36, nonce="n-1")
        self.assertNotIn(" ", ext, "含空格会被判无效")
        parsed = json.loads(ext)
        self.assertEqual(parsed["guid"], ACCT["guid"])
        # 2026-11-18 实测 200 的那组值：reqScene=0 且带 qimei36
        self.assertEqual(parsed["reqScene"], 0)
        self.assertEqual(parsed["taskType"], "user")
        self.assertIs(parsed["isIoa"], False)
        self.assertEqual(parsed["qimei36"], FAKE_QIMEI36)
        self.assertEqual(len(parsed["uskey"]), 504)
        self.assertEqual(parsed["nonce"], "n-1")


class ModelTests(unittest.TestCase):
    def test_display_name_maps_to_slot(self):
        """用户传显示名 → 上游只认槽位名；不归一会 400。"""
        self.assertEqual(
            endpoints.canonical_model_id("deepseek-v4-1-flash-261017"), "main-auto")
        self.assertEqual(
            endpoints.canonical_model_id("claude-sonnet-4-6"), "claude-sonnet-4-6")

    def test_legacy_index_maps_to_slot(self):
        """旧版把目录存成数组下标，老路由不能作废。"""
        self.assertEqual(endpoints.canonical_model_id("0"), "main-auto")
        self.assertEqual(endpoints.canonical_model_id("4"),
                         "hunyuan-turbos-vision")

    def test_slot_passes_through(self):
        self.assertEqual(endpoints.canonical_model_id("main-auto"), "main-auto")

    def test_unavailable_model_is_trimmed_and_cased(self):
        """denylist 比对前要 trim + 小写，否则手工配的 'Kimi-K2.5 ' 会绕过。"""
        self.assertTrue(endpoints.is_unavailable_model("kimi-k2.5"))
        self.assertTrue(endpoints.is_unavailable_model("  Kimi-K2.5 "))
        self.assertFalse(endpoints.is_unavailable_model("main-auto"))

    def test_tier_limited_slots_are_rejected_locally(self):
        """实测拿不到权限的槽位（4130001 无效模型id）要在本地就拦下。

        不拦的后果：用户选中它 → 上游 400 → 界面显示「失败」但看不出原因，
        每次都要打一趟上游才知道这个号没这个模型。
        """
        for slot in ("claude-sonnet-4-6", "claude-opus-4-6", "gpt-5.4"):
            self.assertIn(slot, endpoints.UNAVAILABLE_SLOTS)
            self.assertTrue(endpoints.is_unavailable_model(slot),
                            "%s 应被本地拦下" % slot)
            # 也不该出现在对外目录里
            self.assertNotIn(slot, endpoints.DISPLAY_BY_SLOT)

    def test_catalog_only_lists_reachable_slots(self):
        """对外目录里不能混进拿不到权限的槽位。"""
        catalog_slots = {item["api_id"] for item in endpoints.models_catalog()}
        self.assertFalse(catalog_slots & set(endpoints.UNAVAILABLE_SLOTS))

    def test_catalog_matches_table(self):
        catalog = endpoints.models_catalog()
        self.assertEqual(len(catalog), len(endpoints.MODEL_TABLE))
        for item in catalog:
            self.assertTrue(item["api_id"], "api_id 不能空")
            self.assertTrue(item["id"])
            self.assertGreater(item["context"], 0)

    def test_every_table_slot_is_unique(self):
        slots = [row[0] for row in endpoints.MODEL_TABLE]
        self.assertEqual(len(slots), len(set(slots)))
        names = [row[1] for row in endpoints.MODEL_TABLE]
        self.assertEqual(len(names), len(set(names)))


class AuthUrlTests(unittest.TestCase):
    def test_qq_state_is_decimal(self):
        """QQ 的 state 必须与渲染层一致是**小数**；整数会被判参数无效。"""
        from urllib.parse import urlparse, parse_qs

        url = endpoints.authorize_url("QC", "guid-1")
        query = parse_qs(urlparse(url).query)
        state = query["state"][0]
        self.assertIn(".", state, "state 必须是小数形态：%r" % state)
        self.assertEqual(query["client_id"][0], endpoints.QQ_APP_ID)
        self.assertIn("login_type=QC", query["redirect_uri"][0])

    def test_wx_state_is_guid(self):
        from urllib.parse import urlparse, parse_qs

        url = endpoints.authorize_url("WX", "guid-xyz")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["state"][0], "guid-xyz")
        self.assertEqual(query["appid"][0], endpoints.WX_APP_ID)
        self.assertIn("login_type=WX", query["redirect_uri"][0])


class ImportTests(unittest.TestCase):
    def test_record_accepts_wrapped_credential(self):
        record = marvis_accounts.record_from_payload(
            {"credential": {"openid": "o", "access_token": "t", "guid": "g"}})
        self.assertEqual(record["openid"], "o")
        self.assertEqual(record["access_token"], "t")

    def test_record_rejects_non_json(self):
        with self.assertRaises(ValueError):
            marvis_accounts.record_from_payload("not json at all")

    def test_import_batch_counts_shapes(self):
        """JSON 数组 / JSONL 都要认，且逐条回报 added/updated。"""
        from admin.db import SessionLocal
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from admin.db import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            # 数组形态
            result = marvis_accounts.import_batch(db, json.dumps([
                {"openid": "a1", "access_token": "t1", "guid": "g1", "nickname": "号一"},
                {"openid": "a2", "access_token": "t2", "guid": "g2"},
            ]))
            self.assertEqual(result["added"], 2)
            self.assertEqual(result["failed"], 0)

            # 同一个 openid 再来一次 → 更新而不是新增
            result = marvis_accounts.import_batch(db, json.dumps(
                [{"openid": "a1", "access_token": "t1b", "guid": "g1"}]))
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["updated"], 1)

            # JSONL 形态
            result = marvis_accounts.import_batch(
                db, '{"openid":"a3","access_token":"t3","guid":"g3"}\n'
                    '{"openid":"a4","access_token":"t4","guid":"g4"}\n')
            self.assertEqual(result["added"], 2)

            # 缺 guid 的凭证要被补上（否则永远 -109）
            rows = marvis_accounts.accounts_for(db)
            self.assertTrue(all(marvis_accounts.guid_of(
                marvis_accounts.credentials_of(row)) for row in rows))

    def test_import_reports_missing_fields(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from admin.db import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            result = marvis_accounts.import_batch(
                db, json.dumps([{"openid": "x"}, {"access_token": "y"}]))
            self.assertEqual(result["failed"], 2)
            self.assertEqual(result["added"], 0)
            self.assertTrue(result["errors"])


class RouterHelperTests(unittest.TestCase):
    def test_extract_code_variants(self):
        from admin.platform.marvis.routers.accounts import _extract_code

        self.assertEqual(
            _extract_code("https://x.com/cb?login_type=QC&code=ABC123&state=1.5"),
            "ABC123")
        self.assertEqual(_extract_code("code=ABC123&state=1"), "ABC123")
        self.assertEqual(_extract_code('{"code":"ABC123"}'), "ABC123")
        self.assertEqual(_extract_code("ABC123"), "ABC123")
        self.assertEqual(_extract_code(""), "")

    def test_extract_code_ignores_short_json_code(self):
        """失败响应里的 code 是数字错误码，不能当成授权码。"""
        from admin.platform.marvis.routers.accounts import _extract_code

        self.assertEqual(_extract_code('{"code":-109}'), "")


class GatewayGuardTests(unittest.TestCase):
    def test_unavailable_model_is_404_not_401(self):
        """不可用模型必须 404。401 会让上层把整池账号标成登录失效。"""
        from admin.platform.marvis.gateway import _BadRequest, _validate_model

        with self.assertRaises(_BadRequest) as ctx:
            _validate_model({"model": "kimi-k2.5",
                             "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(ctx.exception.code, "model_not_found")

    def test_unknown_model_rejected_locally(self):
        from admin.platform.marvis.gateway import _BadRequest, _validate_model

        with self.assertRaises(_BadRequest):
            _validate_model({"model": "no-such-model",
                             "messages": [{"role": "user", "content": "hi"}]})

    def test_known_model_passes_and_is_canonicalised(self):
        from admin.platform.marvis.gateway import _validate_model

        slot = _validate_model({"model": "deepseek-v4-1-flash-261017",
                                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(slot, "main-auto")
        self.assertEqual(slot, "main-auto")

    def test_stream_options_include_usage_is_forced(self):
        """不补 include_usage，上游不返回 usage 帧，用量统计永远是 0。"""
        from admin.platform.marvis.gateway import _prepare_body

        prepared = _prepare_body({"model": "x", "stream": True}, "main-auto")
        self.assertEqual(prepared["model"], "main-auto")
        self.assertTrue(prepared["stream_options"]["include_usage"])
        # 非流式不该被塞 stream_options
        prepared = _prepare_body({"model": "x"}, "main-auto")
        self.assertNotIn("stream_options", prepared)


class ErrorClassifyTests(unittest.TestCase):
    """403 的业务码分类（2026-11-15 线上 4100406 被误判成 signature_invalid）。"""

    def test_illegal_request_business_code_is_not_signature_invalid(self):
        kind = client._classify(403, '{"code":4100406,"msg":"非法请求"}')
        self.assertEqual(kind, "upstream_business")

    def test_business_code_as_string_is_parsed(self):
        self.assertEqual(client._classify(403, '{"code":"4100406"}'),
                         "upstream_business")

    def test_403_without_code_is_upstream_business(self):
        """模型网关**不签名**（endpoints.py:15-19）⇒ 403 不可能是签名问题。"""
        self.assertEqual(client._classify(403, ""), "upstream_business")
        self.assertEqual(client._classify(403, "forbidden"), "upstream_business")

    def test_risk_control_code_is_kept(self):
        """4100404 是设备/IP 风控，必须留在 risk_control（不能并进 auth）。"""
        self.assertEqual(client._classify(403, '{"code":4100404,"msg":"风控"}'),
                         "risk_control")

    def test_credential_expired_code_is_auth(self):
        self.assertEqual(
            client._classify(403, '{"code":4100403,"msg":"登录态过期/未登录"}'),
            "auth")

    def test_other_statuses_unchanged(self):
        self.assertEqual(client._classify(401, ""), "auth")
        self.assertEqual(client._classify(429, ""), "rate_limit")
        self.assertEqual(client._classify(502, ""), "upstream_error")
        self.assertEqual(client._classify(400, ""), "http_error")

    def test_biz_code_of_handles_nested_envelope(self):
        self.assertEqual(client._biz_code_of('{"error":{"code":4100406}}'),
                         "4100406")
        self.assertEqual(client._biz_code_of('HTTP 403: {"code":4100406}'),
                         "4100406")
        self.assertEqual(client._biz_code_of(""), "")
        self.assertEqual(client._biz_code_of("not json at all"), "")


class EventLoopSafetyTests(unittest.TestCase):
    """阻塞调用必须在工作线程里（2026-11-15 事件循环被钉死 63.5 秒）。"""

    def test_attempt_is_sync_and_acquire_is_the_one_offloaded(self):
        """`_attempt` 必须是**同步 def**，由同步的 `_acquire` 在线程池里调。

        758ab76 线上事故：合并把 `_attempt` 改成了 `async def`，而 `_acquire`
        仍是同步 `def` —— 工作线程里 `resp, account, started = _attempt(...)`
        拿到的是 coroutine 对象，直接
        `TypeError: cannot unpack non-iterable coroutine object`，marvis 全量 502。
        """
        import inspect

        from admin.platform.marvis import gateway

        self.assertFalse(
            inspect.iscoroutinefunction(gateway._attempt),
            "_attempt 必须是同步 def：它整段都是阻塞调用，只能跑在工作线程里")
        self.assertFalse(
            inspect.iscoroutinefunction(gateway._acquire),
            "_acquire 必须是同步 def：chat_completions 用 to_thread 把它丢进线程池")
        self.assertIn("asyncio.to_thread",
                      inspect.getsource(gateway.chat_completions),
                      "chat_completions 必须用 to_thread 调 _acquire")

    def test_upstream_call_is_sync_inside_worker_thread(self):
        """`_call_upstream` 同步直连 client —— 它本来就跑在工作线程里。

        旧写法（async + 每步 `to_thread`）与上游同步的 `_acquire` 不兼容，
        合并时只改了一半就是 758ab76 的 502。这里钉死「线程里不再套线程」。
        """
        import ast
        import inspect
        import textwrap

        from admin.platform.marvis import gateway

        self.assertFalse(inspect.iscoroutinefunction(gateway._call_upstream))
        # 只看代码，不看 docstring（docstring 里提到 to_thread 是说明文字）。
        node = ast.parse(textwrap.dedent(
            inspect.getsource(gateway._call_upstream))).body[0]
        if (isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)):
            node.body = node.body[1:]
        code = ast.unparse(node)
        self.assertNotIn("asyncio.to_thread", code,
                         "工作线程里再套 to_thread 只会多一次上下文切换")
        self.assertIn("client.open_chat_stream(", code)
        self.assertIn("client.chat_completions(", code)

    def test_stream_reader_goes_through_to_thread(self):
        import inspect

        from admin.platform.marvis import gateway

        src = inspect.getsource(gateway._iter_upstream)
        self.assertIn("asyncio.to_thread", src)

    def test_retry_whitelist_has_no_business_or_auth_kinds(self):
        from admin.platform.marvis import gateway

        self.assertIn("network", gateway.RETRY_SAME_ACCOUNT_KINDS)
        for kind in ("upstream_business", "signature_invalid", "auth",
                     "risk_control"):
            self.assertNotIn(kind, gateway.RETRY_SAME_ACCOUNT_KINDS)

    def test_retry_biz_codes_only_contain_illegal_request(self):
        """业务码白名单**只**放 4100406：4100403 是 auth、4100404 是风控。"""
        from admin.platform.marvis import gateway

        self.assertIn("4100406", gateway.RETRY_SAME_ACCOUNT_BIZ_CODES)
        for code in ("4100403", "4100404"):
            self.assertNotIn(code, gateway.RETRY_SAME_ACCOUNT_BIZ_CODES)
        # 2026-11-16 由 1 提到 2：线上 16:06:44.928 那条 403 elapsed 8869ms
        # ≈ 两次尝试都撞上随机拒绝（单次约 5–10%），1 次重试把失败率压到 ~1%
        # 不够，3 次尝试可压到 ~0.1%。上限仍只由这一个常量控制。
        self.assertEqual(gateway.SAME_ACCOUNT_RETRIES, 2,
                         "最多 3 次尝试（1 次首发 + 2 次同号重试）")


class SameAccountRetryTests(unittest.IsolatedAsyncioTestCase):
    """同号重试：只对白名单 kind，且**复用同一个账号**（不重新选号）。"""

    def setUp(self):
        from admin.platform.marvis import gateway
        from admin.platform.marvis.core import client as mclient

        self.gateway = gateway
        self.mclient = mclient
        self._orig_open = mclient.open_chat_stream
        self._orig_delay = gateway.SAME_ACCOUNT_RETRY_DELAY
        self._orig_jitter = gateway.SAME_ACCOUNT_RETRY_JITTER
        gateway.SAME_ACCOUNT_RETRY_DELAY = 0
        gateway.SAME_ACCOUNT_RETRY_JITTER = 0

    def tearDown(self):
        self.mclient.open_chat_stream = self._orig_open
        self.gateway.SAME_ACCOUNT_RETRY_DELAY = self._orig_delay
        self.gateway.SAME_ACCOUNT_RETRY_JITTER = self._orig_jitter

    async def test_transient_network_error_retries_same_account(self):
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            if len(calls) == 1:
                raise self.mclient.UpstreamError("boom", status=0, kind="network")
            return "STREAM"

        self.mclient.open_chat_stream = fake_open
        got = self.gateway._call_upstream({}, "cn", {}, 7, stream=True)
        self.assertEqual(got, "STREAM")
        self.assertEqual(calls, [7, 7], "必须是同一个账号重试")

    async def test_illegal_request_4100406_retries_same_account_once(self):
        """4100406 是上游**偶发**拒绝（同号同分钟 200/403 交替）⇒ 同号重试。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            if len(calls) == 1:
                raise self.mclient.UpstreamError(
                    "模型网关失败（HTTP 403）", status=403, code="403",
                    kind=self.mclient._classify(
                        403, '{"code":4100406,"msg":"非法请求"}'),
                    detail='{"code":4100406,"msg":"非法请求"}')
            return "STREAM"

        self.mclient.open_chat_stream = fake_open
        got = self.gateway._call_upstream({}, "cn", {}, 7, stream=True)
        self.assertEqual(got, "STREAM")
        self.assertEqual(calls, [7, 7], "4100406 必须同号重试（首发失败后重试成功）")

    async def test_illegal_request_4100406_gives_up_after_two_retries(self):
        """连撞三次 4100406 仍然要抛 —— 不许变成无限重试。

        这里的「3 次」是**单个号**的上限（1 次首发 + 2 次同号重试），
        语义没有变化。整条请求的上限是「3 个号 × 每号 3 次 = 9 次上游调用」，
        由下面的 `AccountSwitchTests` 覆盖（2026-11-16 换号解耦）。
        """
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self.mclient.UpstreamError(
                "模型网关失败（HTTP 403）", status=403, code="403",
                kind="upstream_business",
                detail='{"code":4100406,"msg":"非法请求"}')

        self.mclient.open_chat_stream = fake_open
        with self.assertRaises(self.mclient.UpstreamError):
            self.gateway._call_upstream({}, "cn", {}, 7, stream=True)
        self.assertEqual(len(calls), 3, "只许重试两次（1 次首发 + 2 次重试）")

    async def test_backoff_increases_and_jitters(self):
        """退避 = 基数 × (index + 1) + U(0, 抖动)：递增且带抖动。

        递增是为了避开上游短时抖动窗口；抖动是为了避免同频重发。
        这里把 `time.sleep` / `random.uniform` 换成记录器，只断言「喂给
        sleep 的秒数」—— 不真的睡。

        ⚠ 退避用的是 `time.sleep`（同步）：`_call_upstream` 整段在工作线程里
        跑，睡的是那个线程，事件循环不受影响。
        """
        from unittest import mock

        sleeps = []
        calls = []

        def fake_sleep(seconds):
            sleeps.append(seconds)

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self.mclient.UpstreamError(
                "模型网关失败（HTTP 403）", status=403, code="403",
                kind="upstream_business",
                detail='{"code":4100406,"msg":"非法请求"}')

        self.mclient.open_chat_stream = fake_open
        self.gateway.SAME_ACCOUNT_RETRY_DELAY = 0.3
        self.gateway.SAME_ACCOUNT_RETRY_JITTER = 0.3
        with mock.patch.object(self.gateway.time, "sleep", fake_sleep), \
                mock.patch.object(self.gateway.random, "uniform",
                                  return_value=0.25):
            with self.assertRaises(self.mclient.UpstreamError):
                self.gateway._call_upstream({}, "cn", {}, 7, stream=True)

        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2, "两次重试各退避一次")
        self.assertAlmostEqual(sleeps[0], 0.3 * 1 + 0.25, places=6)
        self.assertAlmostEqual(sleeps[1], 0.3 * 2 + 0.25, places=6)
        self.assertLess(sleeps[0], sleeps[1], "退避必须递增")

    async def test_other_business_error_is_not_retried(self):
        """4100406 之外的业务拒绝仍然不重试（本次只放开这一个码）。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self.mclient.UpstreamError(
                "模型网关失败（HTTP 403）", status=403, code="403",
                kind="upstream_business",
                detail='{"code":4100410,"msg":"请求不合法"}')

        self.mclient.open_chat_stream = fake_open
        with self.assertRaises(self.mclient.UpstreamError):
            self.gateway._call_upstream({}, "cn", {}, 7, stream=True)
        self.assertEqual(len(calls), 1, "非 4100406 的 upstream_business 不许重试")

    async def test_param_error_400_is_not_retried(self):
        """400 参数错：换号没用、重打也没用 ⇒ 一次就抛（不浪费一次 5s 往返）。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self.mclient.UpstreamError(
                "模型网关失败（HTTP 400）", status=400, code="400",
                kind="upstream_business",
                detail='{"code":400,"msg":"参数错误"}')

        self.mclient.open_chat_stream = fake_open
        with self.assertRaises(self.mclient.UpstreamError):
            self.gateway._call_upstream({}, "cn", {}, 7, stream=True)
        self.assertEqual(len(calls), 1, "400 参数错不许重试")

    async def test_auth_kind_with_same_code_is_not_retried(self):
        """kind 是 auth / risk_control 时，即便 detail 里带 4100406 也不重试。"""
        for kind in ("auth", "risk_control"):
            calls = []

            def fake_open(data, realm, body, *, account_id=0):
                calls.append(account_id)
                raise self.mclient.UpstreamError(
                    "模型网关失败（HTTP 403）", status=403, code="403",
                    kind=kind, detail='{"code":4100406,"msg":"非法请求"}')

            self.mclient.open_chat_stream = fake_open
            with self.assertRaises(self.mclient.UpstreamError):
                self.gateway._call_upstream({}, "cn", {}, 7, stream=True)
            self.assertEqual(len(calls), 1, "kind=%s 不许重试" % kind)


class AccountSwitchTests(unittest.IsolatedAsyncioTestCase):
    """换号重试：同号重试**耗尽后必须换下一个号**（2026-11-16 加日志 + 钉死路径）。

    2026-11-16 01:13 的线上 403：一个号连打 3 次（7–8s）后请求直接失败。
    查库确认当时池里**只有一个 marvis 号**（187；189 是 17:14:27 才导入的），
    所以「没换号」不是分支缺失，而是**没号可换** —— 但旧代码一条日志都没有，
    线上根本看不出这一点。这里用假号池把路径钉死：「A 号 3 次全 403、
    B 号首次 200」⇒ 最终成功，顺带钉住 auth 不重试直接换号、400/422 直接抛、
    整条请求最多 9 次上游调用。
    """

    def setUp(self):
        from admin.platform.marvis import gateway
        from admin.platform.marvis.core import client as mclient

        self.gateway = gateway
        self.mclient = mclient
        self._orig = {
            "open": mclient.open_chat_stream,
            "select": gateway.shared.select_account,
            "lease": gateway.shared.lease,
            "release": gateway.shared.release,
            "success": gateway.shared.note_success,
            "failure": gateway.shared.note_failure,
            "ensure": gateway.marvis_accounts.ensure_token,
            "credentials": gateway.marvis_accounts.credentials_of,
            "hint": gateway.marvis_accounts.token_expiry_hint,
            "delay": gateway.SAME_ACCOUNT_RETRY_DELAY,
            "jitter": gateway.SAME_ACCOUNT_RETRY_JITTER,
        }
        gateway.SAME_ACCOUNT_RETRY_DELAY = 0
        gateway.SAME_ACCOUNT_RETRY_JITTER = 0
        self.accounts = [SimpleNamespace(id=1, auth_json="{}"),
                         SimpleNamespace(id=2, auth_json="{}")]
        self.failures = []

        def fake_select(db, *, storage, realm, key, model, exclude_ids=None,
                        min_balance=None):
            excluded = set(exclude_ids or ())
            for account in self.accounts:
                if account.id not in excluded:
                    return account
            return None

        def fake_note_failure(db, account, **kwargs):
            self.failures.append((account.id, kwargs.get("kind"),
                                  kwargs.get("cool_seconds")))

        gateway.shared.select_account = fake_select
        gateway.shared.lease = lambda account: True
        gateway.shared.release = lambda account: None
        gateway.shared.note_success = lambda account: None
        gateway.shared.note_failure = fake_note_failure
        gateway.marvis_accounts.ensure_token = lambda db, account: {"t": "x"}
        gateway.marvis_accounts.credentials_of = lambda account: {}
        gateway.marvis_accounts.token_expiry_hint = lambda credentials: {}

    def tearDown(self):
        self.mclient.open_chat_stream = self._orig["open"]
        self.gateway.shared.select_account = self._orig["select"]
        self.gateway.shared.lease = self._orig["lease"]
        self.gateway.shared.release = self._orig["release"]
        self.gateway.shared.note_success = self._orig["success"]
        self.gateway.shared.note_failure = self._orig["failure"]
        self.gateway.marvis_accounts.ensure_token = self._orig["ensure"]
        self.gateway.marvis_accounts.credentials_of = self._orig["credentials"]
        self.gateway.marvis_accounts.token_expiry_hint = self._orig["hint"]
        self.gateway.SAME_ACCOUNT_RETRY_DELAY = self._orig["delay"]
        self.gateway.SAME_ACCOUNT_RETRY_JITTER = self._orig["jitter"]

    def _illegal_request(self):
        return self.mclient.UpstreamError(
            "模型网关失败（HTTP 403）", status=403, code="403",
            kind="upstream_business",
            detail='{"code":4100406,"msg":"非法请求"}')

    async def _run(self, body=None):
        # `_attempt` 是同步 def（整段跑在工作线程里），这里直接调。
        return self.gateway._attempt(
            None, None, None, body or {"model": "main-auto"}, "cn",
            stream=True)

    async def test_switches_account_after_same_account_retries(self):
        """探针 ①：A 号 3 次全 403、B 号首次 200 ⇒ 最终成功，调用 [A,A,A,B]。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            if account_id == 1:
                raise self._illegal_request()
            return "RESULT"

        self.mclient.open_chat_stream = fake_open
        result, account, _ = await self._run()
        self.assertEqual(result, "RESULT")
        self.assertEqual(account.id, 2)
        self.assertEqual(calls, [1, 1, 1, 2],
                         "A 号同号重试耗尽后必须换到 B 号再试")

    async def test_switch_is_logged(self):
        """要求 4：换号成功要留一条可 grep 的日志。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            if account_id == 1:
                raise self._illegal_request()
            return "RESULT"

        self.mclient.open_chat_stream = fake_open
        with self.assertLogs("admin.platform.marvis.gateway", level="INFO") as logs:
            await self._run()
        joined = "\n".join(logs.output)
        self.assertIn("Marvis 换号重试 acc=1 -> 2", joined)
        self.assertIn("原因 kind=upstream_business code=403", joined)

    async def test_both_accounts_fail_raises_after_six_calls(self):
        """探针 ②：两个号都 403 ⇒ 抛，且每号各 3 次（共 6 次上游调用）。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self._illegal_request()

        self.mclient.open_chat_stream = fake_open
        with self.assertRaises(self.mclient.UpstreamError):
            await self._run()
        self.assertEqual(calls, [1, 1, 1, 2, 2, 2],
                         "每个号各 1 次首发 + 2 次同号重试，然后换号")

    async def test_auth_switches_without_retry_and_cools(self):
        """探针 ③：A 号 401(auth) ⇒ 不重试、直接换 B；A 冷却 600s。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            if account_id == 1:
                raise self.mclient.UpstreamError(
                    "登录态过期", status=401, code="4100403", kind="auth",
                    detail='{"code":4100403,"msg":"登录态过期/未登录"}')
            return "RESULT"

        self.mclient.open_chat_stream = fake_open
        result, account, _ = await self._run()
        self.assertEqual(result, "RESULT")
        self.assertEqual(account.id, 2)
        self.assertEqual(calls, [1, 2], "auth 不许同号重试，直接换号")
        self.assertEqual(self.failures, [(1, "auth", 600)],
                         "auth 必须冷却该号 600s 再换")

    async def test_three_accounts_cap_nine_upstream_calls(self):
        """总上限：MAX_ATTEMPTS=3 个号 × 每号 3 次 = 最多 9 次上游调用。"""
        self.accounts.append(SimpleNamespace(id=3, auth_json="{}"))
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self._illegal_request()

        self.mclient.open_chat_stream = fake_open
        with self.assertRaises(self.mclient.UpstreamError):
            await self._run()
        self.assertEqual(len(calls), 9, "3 个号 × 3 次，不许无限换号")
        self.assertEqual(calls, [1, 1, 1, 2, 2, 2, 3, 3, 3])

    async def test_param_error_400_is_raised_without_switch(self):
        """400/422 换号也没用：一次就抛，不换号。"""
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            raise self.mclient.UpstreamError(
                "模型网关失败（HTTP 400）", status=400, code="400",
                kind="upstream_business",
                detail='{"code":3000101,"msg":"invalid parameter"}')

        self.mclient.open_chat_stream = fake_open
        with self.assertRaises(self.mclient.UpstreamError):
            await self._run()
        self.assertEqual(calls, [1], "400 直接抛，不重试也不换号")

    async def test_request_profile_logged_without_body_content(self):
        """B：请求画像只记数字；用户对话内容绝不进日志。"""
        secret = "用户私有对话内容-不应出现在日志里"
        calls = []

        def fake_open(data, realm, body, *, account_id=0):
            calls.append(account_id)
            return "RESULT"

        self.mclient.open_chat_stream = fake_open
        body = {
            "model": "main-auto",
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": "You are a helpful AI assistant"},
                {"role": "user", "content": secret},
            ],
            "tools": [{"type": "function", "function": {"name": "t"}}],
        }
        with self.assertLogs("admin.platform.marvis.gateway", level="INFO") as logs:
            await self._run(body)
        joined = "\n".join(logs.output)
        self.assertIn("Marvis 请求画像", joined)
        self.assertIn("msgs=2", joined)
        self.assertIn("tools=1", joined)
        self.assertIn("max_tokens=2048", joined)
        self.assertNotIn(secret, joined, "请求画像绝不能打印 body 内容")


class Dump403Tests(unittest.IsolatedAsyncioTestCase):
    """403 请求体转储（临时排障开关）：默认关闭、只对 403、内容绝不进日志。

    线上 4100406 只在大请求上偶发（bytes=3590147 / msgs=270），同形状的合成
    请求全 200 ⇒ 触发条件是**内容**。要二分内容，必须拿到真实失败的那份 body；
    这里钉死转储的四个不变量，免得开关本身成为新的线上风险：

      ① 默认关闭 —— 不设 env 时**一个字节都不写盘**；
      ② 只对 403 —— 500 / 200 不落盘（否则名额被无关失败占满）；
      ③ 落盘字节 == 上游实际收到的字节（`json.dumps(..., ensure_ascii=False)`），
         否则重放的不是同一份请求，二分结论无意义；
      ④ 内容不进日志 —— 日志只有路径与字节数（dump 里是用户对话原文）。
    """

    def setUp(self):
        import tempfile

        from admin.platform.marvis import gateway

        self.gateway = gateway
        self._tmp = tempfile.mkdtemp(prefix="mh403-test-")
        self._env_keys = (gateway.DUMP_403_ENV, gateway.DUMP_403_DIR_ENV,
                          gateway.DUMP_403_KEEP_ENV)
        self._saved_env = {k: os.environ.get(k) for k in self._env_keys}
        self._saved_seen = dict(gateway._DUMP_403_SEEN)
        gateway._DUMP_403_SEEN.clear()
        for key in self._env_keys:
            os.environ.pop(key, None)
        # 端到端用例会真的走到同号重试分支：退避置 0，别让测试真睡。
        self._saved_delay = gateway.SAME_ACCOUNT_RETRY_DELAY
        self._saved_jitter = gateway.SAME_ACCOUNT_RETRY_JITTER
        gateway.SAME_ACCOUNT_RETRY_DELAY = 0
        gateway.SAME_ACCOUNT_RETRY_JITTER = 0

    def tearDown(self):
        import shutil

        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.gateway._DUMP_403_SEEN.clear()
        self.gateway._DUMP_403_SEEN.update(self._saved_seen)
        self.gateway.SAME_ACCOUNT_RETRY_DELAY = self._saved_delay
        self.gateway.SAME_ACCOUNT_RETRY_JITTER = self._saved_jitter
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _enable(self, keep=None):
        os.environ[self.gateway.DUMP_403_ENV] = "1"
        os.environ[self.gateway.DUMP_403_DIR_ENV] = self._tmp
        if keep is not None:
            os.environ[self.gateway.DUMP_403_KEEP_ENV] = str(keep)

    def _files(self):
        return sorted(os.listdir(self._tmp))

    def test_disabled_by_default_writes_nothing(self):
        path = self.gateway._dump_403_body({"messages": []}, status=403)
        self.assertEqual(path, "")
        self.assertEqual(self._files(), [], "默认关闭时不许碰磁盘")

    def test_only_403_is_dumped(self):
        self._enable()
        for status in (200, 400, 500, 502, None):
            self.assertEqual(
                self.gateway._dump_403_body({"messages": []}, status=status), "")
        self.assertEqual(self._files(), [], "只有 403 才落盘")

    def test_dump_bytes_equal_sent_bytes(self):
        """落盘内容必须与 client 真正发出的字节逐字节相同。"""
        import gzip
        import json

        self._enable()
        body = {
            "model": "main-auto",
            "max_tokens": 128000,
            "messages": [
                {"role": "system", "content": "You are a helpful AI assistant"},
                {"role": "user", "content": "中文 + emoji 😀 + \n 换行"},
            ],
            "tools": [{"type": "function", "function": {"name": "t"}}],
        }
        path = self.gateway._dump_403_body(body, status=403, account_id=189)
        self.assertTrue(path.endswith(".json.gz"))
        with gzip.open(path, "rb") as handle:
            raw = handle.read()
        self.assertEqual(raw, json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def test_content_never_reaches_logs(self):
        secret = "用户私有对话内容-绝不应出现在日志里"
        self._enable()
        body = {"messages": [{"role": "user", "content": secret}]}
        with self.assertLogs("admin.platform.marvis.gateway", level="WARNING") as logs:
            path = self.gateway._dump_403_body(body, status=403, account_id=189)
        joined = "\n".join(logs.output)
        self.assertIn("Marvis 403 请求体已转储", joined)
        self.assertIn(os.path.basename(path), joined, "日志要给路径，否则没法取文件")
        self.assertIn("bytes=", joined)
        self.assertNotIn(secret, joined, "转储日志绝不能打印 body 内容")

    def test_same_body_dumped_once(self):
        """同一次请求的重试/换号共享同一份 body ⇒ 只落一份。"""
        self._enable()
        body = {"messages": [{"role": "user", "content": "x"}]}
        first = self.gateway._dump_403_body(body, status=403)
        second = self.gateway._dump_403_body(body, status=403)
        self.assertEqual(first, second)
        self.assertEqual(len(self._files()), 1, "同一份内容不许重复占名额")

    def test_keeps_only_newest_three(self):
        self._enable()
        paths = []
        for index in range(5):
            body = {"messages": [{"role": "user", "content": "n%d" % index}]}
            paths.append(self.gateway._dump_403_body(body, status=403))
        files = self._files()
        self.assertEqual(len(files), 3, "最多保留 3 份")
        kept = {os.path.basename(path) for path in paths[-3:]}
        self.assertEqual(set(files), kept, "留下的是最新的 3 份")

    async def test_upstream_403_triggers_dump_but_500_does_not(self):
        """端到端：`_call_upstream` 的 403 分支要真的把 body 落盘。"""
        from admin.platform.marvis.core import client as mclient

        self._enable()
        orig_open = mclient.open_chat_stream
        self.addCleanup(setattr, mclient, "open_chat_stream", orig_open)

        def open_403(data, realm, body, *, account_id=0):
            raise mclient.UpstreamError(
                "模型网关失败（HTTP 403）", status=403, code="403",
                kind="upstream_business",
                detail='{"code":4100406,"msg":"非法请求"}')

        mclient.open_chat_stream = open_403
        body = {"model": "main-auto",
                "messages": [{"role": "user", "content": "big"}]}
        with self.assertLogs("admin.platform.marvis.gateway", level="WARNING"):
            with self.assertRaises(mclient.UpstreamError):
                self.gateway._call_upstream({}, "cn", body, 189,
                                             stream=True)
        self.assertEqual(len(self._files()), 1, "403 必须落一份，且只落一份")

        os.environ.pop(self.gateway.DUMP_403_ENV, None)
        self.gateway._DUMP_403_SEEN.clear()
        before = set(self._files())

        def open_500(data, realm, body, *, account_id=0):
            raise mclient.UpstreamError(
                "模型网关失败（HTTP 500）", status=500, code="500",
                kind="upstream_error")

        mclient.open_chat_stream = open_500
        os.environ[self.gateway.DUMP_403_ENV] = "1"
        # 500 走 kind=upstream_error ⇒ 不重试、也不落盘；这里只要求「不落盘」。
        with self.assertLogs("admin.platform.marvis.gateway", level="INFO"):
            with self.assertRaises(mclient.UpstreamError):
                self.gateway._call_upstream({}, "cn", body, 189,
                                             stream=True)
        self.assertEqual(set(self._files()), before, "500 不许落盘")


class DispatchModelForPlatformTests(unittest.TestCase):
    """dispatch 的「这个模型属于该平台吗」校验（2026-11-15 线上 404 回归）。

    线上 4/5 个 marvis 模型恒 404：客户端发 `deepseek-v4.1-flash-marvis`，
    改写链把名字换成上游槽位名 `main-auto`，而 `model_configs` 存的是
    **可读名**（`deepseek-v4.1-flash`）—— 校验只比对清单里的原始写法，
    槽位名永远不中，于是判成 `model_not_found`。

    ⚠️ 必须用**全局库**（`SessionLocal`）：改写链里的 `is_native_model_id()`
    与 `_model_id_for()` 内部自己开 session，喂一个私有内存库它们看不见，
    会退回旧行为 —— 那样测试就测不到真实的线上链路。
    """

    MODELS = ("deepseek-v4.1-flash", "deepseek-v4-pro",
              "deepseek-v4-flash", "hy3", "hunyuan-vision-2.0")
    #: 客户端实际发的名字（带平台后缀）
    CLIENT_NAMES = ("deepseek-v4.1-flash-marvis", "deepseek-v4-pro-marvis",
                    "deepseek-v4-flash-marvis", "hy3-marvis")
    #: 改写链会产出的上游槽位名
    SLOTS = ("main-auto", "deepseek-v4-pro-external",
             "hy3-preview-agent", "hy3-preview-agent-real")

    def setUp(self):
        from admin.db import Base, SessionLocal, engine
        from admin.models import ModelConfig
        from admin.platform import dispatch, models_router

        Base.metadata.create_all(engine)
        self._db = SessionLocal()
        self._dispatch = dispatch
        self._models_router = models_router
        # 清掉上一次运行残留的 marvis 行，保证清单可预期
        self._db.query(ModelConfig).filter(
            ModelConfig.platform == "marvis").delete()
        self._db.add_all([
            ModelConfig(platform="marvis", model_id=name, enabled=1,
                        credit_multiplier=1.0, upstream_metadata="{}")
            for name in self.MODELS
        ])
        self._db.commit()
        # 两处缓存都是模块级的，必须清掉，否则读到别的用例留下的快照。
        models_router._native_cache.update({"at": 0.0, "ids": {}})
        dispatch.invalidate_model_cache()

    def tearDown(self):
        dispatch = getattr(self, "_dispatch", None)
        router = getattr(self, "_models_router", None)
        db = getattr(self, "_db", None)
        if db is not None:
            from admin.models import ModelConfig

            db.query(ModelConfig).filter(
                ModelConfig.platform == "marvis").delete()
            db.commit()
            db.close()
        if router is not None:
            router._native_cache.update({"at": 0.0, "ids": {}})
        if dispatch is not None:
            dispatch.invalidate_model_cache()

    def test_client_names_pass_the_rewrite_chain(self):
        """4 个线上挂掉的 marvis 名字，走完整改写链后必须不再 404。"""
        from admin.platform import model_suffix

        for name in self.CLIENT_NAMES:
            _, bare, _ = model_suffix.resolve(name)
            _, upstream = model_suffix.alias_to_upstream(name)
            _, error = self._dispatch._model_for_platform(
                self._db, "marvis", upstream or bare or name)
            self.assertIsNone(error, "%s 被判成 %r" % (name, error))

    def test_slot_names_are_mapped_not_rejected(self):
        """裸槽位名（老客户端配置里的写法）也要认，并映射回可读名。"""
        for slot in self.SLOTS:
            mapped, error = self._dispatch._model_for_platform(
                self._db, "marvis", slot)
            self.assertIsNone(error, "槽位名 %s 被判成 %r" % (slot, error))
            self.assertIn(mapped, self.MODELS,
                          "槽位名 %s 应映射到可读名，实际 %r" % (slot, mapped))

    def test_unknown_model_is_still_rejected(self):
        """真不存在的模型必须继续 404 —— 别把校验顺手改成「全放行」。"""
        _, error = self._dispatch._model_for_platform(
            self._db, "marvis", "no-such-model-xyz")
        self.assertIsNotNone(error, "不存在的模型必须被拒")
        self.assertEqual(error["code"], "model_not_found")


class QuotaExhaustedTests(unittest.IsolatedAsyncioTestCase):
    """额度耗尽必须报 402 `quota_exhausted`，不是 503 `accounts_cooling`。

    2026-11-18 事故：三个号的上游日配额（各 10,000,000 token）同时打满，
    选号用 `min_balance=0` 把余额 0 的号全滤掉 ⇒ 「一个号都没选到」被报成
    503「都在冷却或排队中，请稍后重试」。用户照着这句话等冷却，而真正要等的
    是上游 00:00(+08) 的配额重置 —— 长则十几小时。
    """

    def setUp(self):
        from admin.platform import shared
        from admin.platform.marvis import gateway
        from admin.platform.marvis.core import client as mclient

        self.gateway = gateway
        self.shared = shared
        self.mclient = mclient
        self._orig = {
            "open": mclient.open_chat_stream,
            "select": shared.select_account,
            "lease": shared.lease,
            "release": shared.release,
            "success": shared.note_success,
            "failure": shared.note_failure,
            "ensure": gateway.marvis_accounts.ensure_token,
            "accounts": gateway.marvis_accounts.accounts_for,
            "delay": gateway.SAME_ACCOUNT_RETRY_DELAY,
            "jitter": gateway.SAME_ACCOUNT_RETRY_JITTER,
        }
        gateway.SAME_ACCOUNT_RETRY_DELAY = 0
        gateway.SAME_ACCOUNT_RETRY_JITTER = 0
        shared.lease = lambda account: True
        shared.release = lambda account: None
        shared.note_success = lambda account: None
        shared.note_failure = lambda db, account, **kw: None
        gateway.marvis_accounts.ensure_token = lambda db, account: {"t": "x"}

    def tearDown(self):
        g = self.gateway
        g.shared.select_account = self._orig["select"]
        g.shared.lease = self._orig["lease"]
        g.shared.release = self._orig["release"]
        g.shared.note_success = self._orig["success"]
        g.shared.note_failure = self._orig["failure"]
        g.marvis_accounts.ensure_token = self._orig["ensure"]
        g.marvis_accounts.accounts_for = self._orig["accounts"]
        self.mclient.open_chat_stream = self._orig["open"]
        g.SAME_ACCOUNT_RETRY_DELAY = self._orig["delay"]
        g.SAME_ACCOUNT_RETRY_JITTER = self._orig["jitter"]

    def _pool(self, *balances):
        """造一个「有号但余额如上」的号池；select_account 一律返回 None。"""
        rows = [SimpleNamespace(id=189 + i, status="active",
                                balance_remain=value, last_err_msg="")
                for i, value in enumerate(balances)]
        self.gateway.shared.select_account = lambda db, **kw: None
        self.gateway.marvis_accounts.accounts_for = lambda db, realm="": rows
        return rows

    async def test_all_zero_balance_raises_402_quota_exhausted(self):
        """alive 全 avail=0 ⇒ 402 quota_exhausted（不是 503 accounts_cooling）。"""
        self._pool(0, 0, 0)
        with self.assertRaises(self.shared.PlatformRejected) as caught:
            self.gateway._attempt(None, None, None, {"model": "main-auto"},
                                  "cn", stream=False)
        exc = caught.exception
        self.assertEqual(exc.status, 402, "额度耗尽必须 402，不是 503")
        self.assertEqual(exc.code, "quota_exhausted")
        self.assertEqual(exc.kind, "quota_exceeded")
        self.assertIn("额度已耗尽", exc.message)
        self.assertIn("10,000,000", exc.message, "文案要给出重置口径")
        self.assertEqual(exc.headers.get("Retry-After"), "3600")

    async def test_unknown_balance_is_not_reported_as_exhausted(self):
        """余额 NULL（从没同步过）= 未知，不等于耗尽：仍按冷却 503 重试。"""
        self._pool(None, None)
        with self.assertRaises(self.shared.PlatformRejected) as caught:
            self.gateway._attempt(None, None, None, {"model": "main-auto"},
                                  "cn", stream=False)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "accounts_cooling")

    async def test_partial_balance_still_selects_account(self):
        """alive 里还有 avail>0 的号 ⇒ 正常选号（不许被额度判断挡掉）。"""
        rows = self._pool(0, 6180000)
        picked = []

        def fake_select(db, **kw):
            picked.append(1)
            return SimpleNamespace(id=190, auth_json="{}")

        self.gateway.shared.select_account = fake_select
        # `_attempt` 是同步 def；`stream=True` 时上游走 `open_chat_stream`。
        self.mclient.open_chat_stream = lambda *a, **kw: "RESULT"
        result, account, _started = self.gateway._attempt(
            None, None, None, {"model": "main-auto"}, "cn", stream=True)
        self.assertEqual(result, "RESULT")
        self.assertEqual(account.id, 190)
        self.assertEqual(picked, [1], "有余额的号必须照常被选中")
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)