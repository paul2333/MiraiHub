"""Marvis uskey 签名侧车 + 模型网关请求头：协议层回归。

这里锁的都是「写错了不报错、只静默 4100404」的地方：

  * bus_info 七字段顺序错一个 → F(bus_info) 就错，整块 uskey 被上游拒；
  * MarvisExt 缺 qimei36 / reqScene 写成 1 / 带空格 → 服务端按原文比对，静默无效；
  * Ual-Access-Login-Type 透传 DB 里的 `QC` → 上游判参数无效；
  * 签名不可用时伪造一块 uskey 硬发 → 把「签名故障」变成「账号被风控」；
  * 撞 4100404 还复用同一组 cid/rid/nonce → 继续撞同一个判定。

真实签名的正确性（Wine + beacon_napi 整块 uskey、实发 200）由 VPS 上的
实发用例保证，见 deploy/marvis-uskey-sidecar/README.md。
"""
import base64
import importlib
import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path

# 必须早于 admin.* 导入（admin/db.py 在模块级按 settings 建引擎）。
os.environ.setdefault(
    "ADMIN_DATABASE_URL",
    "sqlite:///" + str(Path(os.environ.get("TEMP", "/tmp")) / "marvis_sidecar_test.db"),
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin.platform.marvis.core import client, endpoints, uskey  # noqa: E402

#: Wine 侧车实测报的 qimei36（真机是 84097bfe4bec892cb32e99d730001bd1a611）。
QIMEI36 = "3aeda2c4343993fc64eec8c830001591ab11"
ACCT = {"openid": "openid-abc", "access_token": "tok-xyz",
        "guid": "64e69d95-c76d-4de9-994b-6285357e6dd4", "login_type": "QC"}


def fake_uskey(nbytes=376):
    """结构合法的假 uskey（magic + 全零尾），只用于长度/校验类断言。"""
    return base64.b64encode(bytes.fromhex("0ac0d50c2111") + bytes(nbytes - 6)).decode()


def random_uskey(nbytes=376):
    """每次不同 —— 用来断言「整组轮换」时 uskey 确实换了。"""
    return base64.b64encode(bytes.fromhex("0ac0d50c2111")
                            + os.urandom(nbytes - 6)).decode()


def make_signer(sink=None):
    def _sign(**kwargs):
        if sink is not None:
            sink.append(kwargs)
        return {"uskey": random_uskey(), "qimei36": QIMEI36, "bus_info": "",
                "uskey_bytes": 376, "uskey_len": 504, "ms": 0}
    return _sign


class IdentityTests(unittest.TestCase):
    def test_bus_info_seven_fields_in_fixed_order(self):
        got = uskey.bus_info(
            agent_tag="main", conversation_id="conv_aa", response_id="rid-1",
            scene="agent_chat", model="main-auto", nonce="non-1", openid="op-1")
        self.assertEqual(
            got,
            "marvisAgentTag=main&marvisConversationID=conv_aa"
            "&marvisResponseID=rid-1&marvisScene=agent_chat"
            "&model=main-auto&nonce=non-1&originOpenID=op-1")

    def test_new_identity_shape_and_uniqueness(self):
        a = uskey.new_identity()
        b = uskey.new_identity()
        self.assertTrue(a["conversation_id"].startswith("conv_"))
        self.assertEqual(len(a["conversation_id"]), 5 + 32)
        int(a["conversation_id"][5:], 16)
        self.assertEqual(len(a["response_id"]), 36)
        self.assertEqual(len(a["nonce"]), 36)
        self.assertNotEqual(a["conversation_id"], b["conversation_id"])
        self.assertNotEqual(a["nonce"], b["nonce"])


class MarvisExtTests(unittest.TestCase):
    def test_ext_key_order_matches_capture(self):
        ext = endpoints.marvis_ext(
            ACCT, uskey_value="U" * 504, qimei36=QIMEI36, nonce="n-1")
        self.assertEqual(list(json.loads(ext).keys()), [
            "qimei36", "osVersion", "guid", "uskey", "nonce", "reqScene",
            "taskType", "clientVersion", "clientPlatform",
            "clientPlatformVersion", "isIoa"])

    def test_ext_values(self):
        parsed = json.loads(endpoints.marvis_ext(
            ACCT, uskey_value="U" * 504, qimei36=QIMEI36, nonce="n-1"))
        self.assertEqual(parsed["qimei36"], QIMEI36)
        self.assertEqual(parsed["reqScene"], 0)
        self.assertEqual(parsed["clientVersion"], "1.60.3400.795")
        self.assertEqual(parsed["clientPlatform"], "Windows")
        self.assertEqual(parsed["clientPlatformVersion"], "1.60.3400.35")
        # 真客户端 200 抓包口径（2026-11-18）：.29695，不是 .26200
        self.assertEqual(parsed["osVersion"], "Windows-10-10.0.29695")

    def test_ext_has_no_whitespace(self):
        ext = endpoints.marvis_ext(ACCT, uskey_value="U" * 504,
                                   qimei36=QIMEI36, nonce="n-1")
        self.assertNotIn(" ", ext)
        self.assertNotIn("\n", ext)


class LoginTypeTests(unittest.TestCase):
    def test_qc_maps_to_one(self):
        self.assertEqual(endpoints.login_type_header({"login_type": "QC"}), "1")
        self.assertEqual(endpoints.login_type_header({"login_type": "qc"}), "1")

    def test_wx_maps_to_two(self):
        self.assertEqual(endpoints.login_type_header({"login_type": "WX"}), "2")

    def test_digit_passthrough_and_default(self):
        self.assertEqual(endpoints.login_type_header({"login_type": "2"}), "2")
        self.assertEqual(endpoints.login_type_header({"login_type": ""}), "1")
        self.assertEqual(endpoints.login_type_header({}), "1")


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.sink = []
        uskey.set_signer(make_signer(self.sink))
        self.addCleanup(uskey.set_signer, None)

    def test_headers_and_ext_share_one_identity(self):
        headers = endpoints.llm_headers(ACCT, model="main-auto")
        kwargs = self.sink[-1]
        ext = json.loads(headers["Ual-Access-MarvisExt"])
        self.assertEqual(headers["Marvis-Conversation-ID"],
                         kwargs["conversation_id"])
        self.assertEqual(headers["Marvis-Response-ID"], kwargs["response_id"])
        self.assertEqual(ext["nonce"], kwargs["nonce"])
        self.assertEqual(headers["Marvis-Scene"], kwargs["scene"])
        self.assertEqual(kwargs["openid"], ACCT["openid"])
        self.assertEqual(kwargs["model"], "main-auto")
        # 模型网关真品发 6（业务接口仍走 QC→1 / WX→2）
        self.assertEqual(headers["Ual-Access-Login-Type"], "6")
        # 真客户端这两个头恒等，且同一个 rid 参与 bus_info 的 marvisResponseID
        self.assertEqual(headers["Ual-Access-Requestid"],
                         headers["Marvis-Response-ID"])

    def test_first_party_header_shape(self):
        headers = endpoints.llm_headers(ACCT, model="main-auto")
        self.assertEqual(headers["User-Agent"], "AsyncOpenAI/Python 2.32.0")
        self.assertEqual(headers["Authorization"], "Bearer placeholder")
        self.assertEqual(headers["Marvis-Protocol-Version"], "1000")
        self.assertEqual(headers["X-Device-GUID"], ACCT["guid"])
        for key in ("X-Stainless-Lang", "X-Stainless-Package-Version",
                    "X-Stainless-OS", "X-Stainless-Arch", "X-Stainless-Runtime",
                    "X-Stainless-Runtime-Version", "X-Stainless-Async",
                    "X-Stainless-Raw-Response"):
            self.assertIn(key, headers)
        # 模型网关绝不能带业务签名头（会把 403 风控变成 401 登录失效）
        self.assertNotIn("Ual-Access-Signature", headers)

    def test_model_goes_into_bus_info(self):
        endpoints.llm_headers(ACCT, model="claude-sonnet-4-6")
        self.assertEqual(self.sink[-1]["model"], "claude-sonnet-4-6")

    def test_each_call_rotates_whole_group(self):
        a = endpoints.llm_headers(ACCT)
        b = endpoints.llm_headers(ACCT)
        self.assertNotEqual(a["Marvis-Conversation-ID"],
                            b["Marvis-Conversation-ID"])
        self.assertNotEqual(a["Marvis-Response-ID"], b["Marvis-Response-ID"])
        self.assertNotEqual(json.loads(a["Ual-Access-MarvisExt"])["nonce"],
                            json.loads(b["Ual-Access-MarvisExt"])["nonce"])


class SidecarFailureTests(unittest.TestCase):
    def tearDown(self):
        uskey.set_signer(None)

    def test_sidecar_error_propagates_and_never_fabricates(self):
        """契约 2026-11-18 变更：默认路径已是本地 mint，本用例改在"强制侧车"模式下
        锁**同一句话**——侧车死就给 SidecarError，绝不退化成伪造 uskey。

        断言一字未改，只是把模式显式钉到 MARVIS_USKEY_FORCE_SIDECAR=1；
        默认模式下"本地 mint 也失败"时的同一个断言见
        test_local_mint_failure_falls_back_to_sidecar。
        """
        orig = uskey.SIDECAR_URL
        uskey.SIDECAR_URL = "http://127.0.0.1:1/uskey"
        uskey.set_signer(None)
        os.environ["MARVIS_USKEY_FORCE_SIDECAR"] = "1"
        self.addCleanup(os.environ.pop, "MARVIS_USKEY_FORCE_SIDECAR", None)
        try:
            with self.assertRaises(uskey.SidecarError):
                uskey.sign(agent_tag="main", conversation_id="conv_x",
                           response_id="rid", scene="agent_chat",
                           model="main-auto", nonce="n", openid="op")
        finally:
            uskey.SIDECAR_URL = orig

    def test_local_mint_is_default_and_needs_no_sidecar(self):
        """侧车**完全不可达**时默认路径照样出合法 uskey（376 字节 / 504 字符）。

        这条锁的是本次改动本身：uskey 不再依赖 Wine 侧车的进程内状态，
        dev18 每次随机，qimei36 来自配置常量。
        """
        orig = uskey.SIDECAR_URL
        uskey.SIDECAR_URL = "http://127.0.0.1:1/uskey"     # 侧车死掉也不许影响
        uskey.set_signer(None)
        try:
            one = uskey.sign(agent_tag="main", conversation_id="conv_1",
                             response_id="rid-1", scene="agent_chat",
                             model="main-auto", nonce="n-1", openid="op",
                             account_key="acc-196")
            two = uskey.sign(agent_tag="main", conversation_id="conv_2",
                             response_id="rid-2", scene="agent_chat",
                             model="main-auto", nonce="n-2", openid="op",
                             account_key="acc-196")
            other = uskey.sign(agent_tag="main", conversation_id="conv_3",
                               response_id="rid-3", scene="agent_chat",
                               model="main-auto", nonce="n-3", openid="op",
                               account_key="acc-197")
        finally:
            uskey.SIDECAR_URL = orig
        self.assertEqual(one["uskey_bytes"], 376)
        self.assertEqual(one["uskey_len"], 504)
        self.assertTrue(one["uskey"].startswith("CsDVDCE"))
        self.assertEqual(len(one["qimei36"]), 36)
        self.assertEqual(one["qimei36"], uskey.qimei36("acc-196"))
        self.assertEqual(len(uskey.qimei36("acc-196")), 36)
        # 不同账号必须得到不同设备串（设备簇事故回归锁）
        self.assertNotEqual(one["qimei36"], other["qimei36"])
        with self.assertRaises(ValueError):
            uskey.qimei36()
        self.assertNotEqual(one["uskey"], two["uskey"])     # dev18 随机 ⇒ 不复用
        self.assertIn("marvisAgentTag=main", one["bus_info"])

    def test_local_mint_failure_falls_back_to_sidecar(self):
        """本地 mint 抛异常时必须回落侧车；两条都不可用才抛 SidecarError。"""
        from admin.platform.marvis.core import uskey_local
        orig_mint = uskey_local.mint_uskey
        orig_url = uskey.SIDECAR_URL

        def boom(*a, **k):
            raise RuntimeError("mint boom")

        uskey_local.mint_uskey = boom
        uskey.SIDECAR_URL = "http://127.0.0.1:1/uskey"
        uskey.set_signer(None)
        self.addCleanup(setattr, uskey_local, "mint_uskey", orig_mint)
        self.addCleanup(setattr, uskey, "SIDECAR_URL", orig_url)
        with self.assertRaises(uskey.SidecarError) as ctx:
            uskey.sign(agent_tag="main", conversation_id="conv_x",
                       response_id="rid", scene="agent_chat",
                       model="main-auto", nonce="n", openid="op")
        self.assertIn("侧车", str(ctx.exception))

    def test_validate_rejects_short_uskey(self):
        with self.assertRaises(uskey.SidecarError):
            uskey._validate({"uskey": base64.b64encode(b"\x00" * 16).decode(),
                             "qimei36": QIMEI36})

    def test_validate_rejects_error_payload(self):
        with self.assertRaises(uskey.SidecarError):
            uskey._validate({"error": "boom"})

    def test_validate_rejects_wine_length(self):
        # Wine 侧车老产物 480 字符 / 360 字节是旧形态（无 320 字节业务 TLV），
        # 376 阈值下必须在本地就拦掉；要排障时用 MARVIS_USKEY_MIN_BYTES 临时放行。
        with self.assertRaises(uskey.SidecarError):
            uskey._validate({"uskey": fake_uskey(360), "qimei36": QIMEI36})
        orig = uskey.USKEY_MIN_BYTES
        uskey.USKEY_MIN_BYTES = 200
        self.addCleanup(setattr, uskey, "USKEY_MIN_BYTES", orig)
        ok = uskey._validate({"uskey": fake_uskey(360), "qimei36": QIMEI36})
        self.assertEqual(ok["uskey_bytes"], 360)
        self.assertEqual(ok["uskey_len"], 480)
        self.assertEqual(ok["qimei36"], QIMEI36)


class UskeyLengthThresholdTests(unittest.TestCase):
    """字节下限 = 真品口径 376（504 字符 base64）。

    历史教训分两段：先是 300 把 264 字节判死 → 全线 502，于是降到 240 放行；
    2026-11-18 真客户端 200 抓包证明真品是 376 字节，264 那版是「旧形态 + 仿的
    身份」（SDK 1.0.0.0 / node.exe / Not Specified、没有 320 字节业务 TLV），
    上游收紧后一律 4100404。所以现在默认 376：本地 mint 只产出 376，任何短包都是
    bug，必须在本地拦死，别拿 403 去污染号池。MARVIS_USKEY_MIN_BYTES 可临时覆盖。
    """

    def setUp(self):
        self._orig_min = uskey.USKEY_MIN_BYTES
        self._orig_env = os.environ.get("MARVIS_USKEY_MIN_BYTES")
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("MARVIS_USKEY_MIN_BYTES", None)
        if self._orig_env is not None:
            os.environ["MARVIS_USKEY_MIN_BYTES"] = self._orig_env
        importlib.reload(uskey)
        if self._orig_env is None:
            # reload 会把值拉回默认 376；显式还原到进用例前的值，避免污染其他用例
            uskey.USKEY_MIN_BYTES = self._orig_min

    def test_validate_accepts_376_byte_uskey(self):
        # 真品口径：376 字节 / 504 字符
        ok = uskey._validate({"uskey": fake_uskey(376), "qimei36": QIMEI36})
        self.assertEqual(ok["uskey_bytes"], 376)
        self.assertEqual(ok["uskey_len"], 504)
        self.assertEqual(ok["qimei36"], QIMEI36)

    def test_validate_rejects_264_byte_uskey(self):
        # 旧形态（208 字节业务 TLV）现在本地就拦，别发出去换 403
        with self.assertRaises(uskey.SidecarError) as ctx:
            uskey._validate({"uskey": fake_uskey(264), "qimei36": QIMEI36})
        self.assertIn("264", str(ctx.exception))

    def test_default_threshold_is_376(self):
        os.environ.pop("MARVIS_USKEY_MIN_BYTES", None)
        importlib.reload(uskey)
        self.assertEqual(uskey.USKEY_MIN_BYTES, 376)

    def test_env_override_raises_threshold(self):
        os.environ["MARVIS_USKEY_MIN_BYTES"] = "512"
        importlib.reload(uskey)
        self.assertEqual(uskey.USKEY_MIN_BYTES, 512)
        with self.assertRaises(uskey.SidecarError):
            uskey._validate({"uskey": fake_uskey(264), "qimei36": QIMEI36})

    def test_env_override_lowers_threshold(self):
        os.environ["MARVIS_USKEY_MIN_BYTES"] = "200"
        importlib.reload(uskey)
        self.assertEqual(uskey.USKEY_MIN_BYTES, 200)
        ok = uskey._validate({"uskey": fake_uskey(239), "qimei36": QIMEI36})
        self.assertEqual(ok["uskey_bytes"], 239)


class _FakeResp:
    def __init__(self):
        self.closed = False

    def read(self):
        return b""

    def close(self):
        self.closed = True


class RiskRetryTests(unittest.TestCase):
    """4100404 **一次都不许重试**；别的错误同样一分钱都不重试。

    2026-11-18 改了契约（旧断言是「整组轮换重试一次」）：

      18:19:11  acc=192 → 4100404
      18:19:13  acc=192 → 4100404   ← 就是这个整组轮换重试
      18:23     acc=192 → 401 4100403（登录态被打死，要重新扫码）

    换 cid/rid/nonce/uskey 改不了设备簇 / IP 级判定，2 秒内重放同一份 body
    只会把风控计数推上去。现在的契约是「单发 + 该号 300 秒冷却」。
    """

    def setUp(self):
        uskey.set_signer(make_signer())
        self.addCleanup(uskey.set_signer, None)
        self.orig_opener = client._opener
        self.orig_sentinel = client.note_sentinel
        client.note_sentinel = lambda *a, **k: None
        client.clear_account_risk_cooldown(0)
        self.addCleanup(client.clear_account_risk_cooldown, 0)
        self.seen = []

    def tearDown(self):
        client._opener = self.orig_opener
        client.note_sentinel = self.orig_sentinel

    def _install(self, payloads):
        seen = self.seen
        itertools_payloads = list(payloads)

        class Opener:
            def open(self, req, timeout=None):
                seen.append({k.lower(): v for k, v in req.header_items()})
                if len(seen) <= len(itertools_payloads):
                    raise urllib.error.HTTPError(
                        req.full_url, 403, "Forbidden", {},
                        io.BytesIO(itertools_payloads[len(seen) - 1]))
                return _FakeResp()

        client._opener = lambda: Opener()

    def test_4100404_is_single_shot_and_cools_the_account(self):
        self._install([b'{"code":4100404,"msg":"\xe8\xaf\xb7\xe6\xb1\x82'
                       b'\xe8\xa2\xab\xe9\xa3\x8e\xe6\x8e\xa7\xe6\x8b\xa6'
                       b'\xe6\x88\xaa"}'])
        with self.assertRaises(client.UpstreamError) as caught:
            client.open_chat_stream(ACCT, "cn",
                                    {"model": "main-auto", "messages": []},
                                    account_id=192)
        # 只打了一发：旧的「整组轮换」在这里会变成 2
        self.assertEqual(len(self.seen), 1)
        exc = caught.exception
        self.assertEqual(exc.kind, "risk_control")
        self.assertEqual(exc.code, "4100404")
        self.assertEqual(exc.cool_seconds, 300)
        self.assertGreater(client.account_risk_cooldown_remaining(192), 0)

    def test_4100404_second_call_is_blocked_without_upstream(self):
        self._install([b'{"code":4100404}', b'{"code":4100404}'])
        with self.assertRaises(client.UpstreamError):
            client.open_chat_stream(ACCT, "cn", {"model": "main-auto"},
                                    account_id=192)
        with self.assertRaises(client.UpstreamError) as caught:
            client.open_chat_stream(ACCT, "cn", {"model": "main-auto"},
                                    account_id=192)
        self.assertEqual(len(self.seen), 1, "冷却期内又打上游了")
        self.assertEqual(caught.exception.kind, "risk_control")
        self.assertEqual(caught.exception.status, 429)

    def test_other_errors_are_not_retried(self):
        self._install([b'{"code":4100403}'])
        with self.assertRaises(client.UpstreamError):
            client.open_chat_stream(ACCT, "cn", {"model": "main-auto"})
        self.assertEqual(len(self.seen), 1)
