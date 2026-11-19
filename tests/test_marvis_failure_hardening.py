
"""Marvis 失败模式全覆盖（2026-11-19）—— 五个口子各一组断言。

口径：**断言行为契约，不断言实现细节**。
  · ① 3000100 -> 先降 max_tokens 同号重试一次；仍 3000100 就标 (账号×模型)
    额度紧张并**换号**，池子用完才把 400（含 upstream_code）报给用户；
  · ② 5100007 -> 同号重试一次，**不换号、不计 err_count、不打冷却**；
  · ③ acquire 超时 -> 504 / 流式错误帧，日志里有 acquire_timeout；
  · ④ 流式错误 -> data: {"error":{...}} + data: [DONE]，且是第一帧；
  · ⑤ >4MB -> ERROR 级日志带 acc/model/msgs。

真机 HTTP 证据（真实 socket、真实 urllib、真实 SSE 字节）由
_harden/e2e_probe.py 产出，不放在这里 —— 单测不碰网络。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin import upstream_http  # noqa: E402
from admin.platform import shared  # noqa: E402
from admin.platform.marvis import gateway  # noqa: E402
from admin.platform.marvis.core import client as marvis_client  # noqa: E402


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
class _FakeAccount:
    def __init__(self, account_id: int):
        self.id = account_id
        self.status = "active"
        self.balance_remain = 510504
        self.balance_total = 10000000
        self.cool_until = None
        self.cool_kind = ""
        self.degrade_until = None
        self.err_count = 0
        self.last_err_at = None
        self.last_err_msg = ""


class _FakeDB:
    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeStream:
    """够 client.close_stream 用。"""

    def __init__(self):
        self.closed = False

    def read(self, _n=-1):
        return b""

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    gateway.reset_risk_backoff()
    upstream_http.reset_platform_openers()
    for name in ("MIRAIHUB_MARVIS_PROXY", "MIRAIHUB_MARVIS_MAX_BODY_BYTES",
                 gateway.RISK_BACKOFF_ENV, gateway.ACQUIRE_TIMEOUT_ENV,
                 gateway.STREAM_STATUS_GRACE_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    gateway.reset_risk_backoff()
    upstream_http.reset_platform_openers()


@pytest.fixture
def note_calls(monkeypatch):
    calls = []
    real = shared.note_failure

    def fake(db, account, **kwargs):
        calls.append(dict(kwargs))
        kwargs.pop("public_platform", None)
        return real(db, account, public_platform="", **kwargs)

    monkeypatch.setattr(gateway.shared, "note_failure", fake)
    return calls


def _install_pool(monkeypatch, accounts):
    selected = []

    def select_account(db, **kwargs):
        excluded = set(kwargs.get("exclude_ids") or ())
        selected.append(frozenset(excluded))
        for account in accounts:
            if account.id not in excluded:
                return account
        return None

    released = []
    monkeypatch.setattr(gateway.shared, "select_account", select_account)
    monkeypatch.setattr(gateway.shared, "lease", lambda account: True)
    monkeypatch.setattr(gateway.shared, "release",
                        lambda account: released.append(
                            int(getattr(account, "id", 0) or 0)))
    monkeypatch.setattr(gateway.shared, "note_success", lambda account: None)
    monkeypatch.setattr(gateway.marvis_accounts, "ensure_token",
                        lambda db, account: {"openid": "o-%s" % account.id})
    return selected, released


def _err(code: str, status: int, kind: str, msg: str = ""):
    return marvis_client.UpstreamError(
        msg or "upstream %s" % code, status=status, code=code, kind=kind,
        detail=json.dumps({"code": code, "msg": msg or code}))


def _max_input_error():
    return _err("3000100", 400, "max_input_tokens", "max input token error")


def _internal_error():
    return _err("5100007", 500, "upstream_internal",
                "服务端通用错误:服务端严重报错")


def _scope(path="/v1/chat/completions"):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }


def _request(body: dict):
    from starlette.requests import Request

    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(_scope(), receive)


async def _read(response):
    """直接消费响应体，返回 (status, headers, body_bytes)。

    不走 ASGI 调用链：这里要断言的是**字节与头部**，不是框架行为；直接读
    body_iterator / body 也不受 Starlette 版本差异影响。
    """
    chunks = []
    iterator = getattr(response, "body_iterator", None)
    if iterator is not None:
        async for chunk in iterator:
            chunks.append(chunk if isinstance(chunk, bytes)
                          else str(chunk).encode("utf-8"))
    else:
        chunks.append(bytes(getattr(response, "body", b"")))
    headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    return int(getattr(response, "status_code", 200)), headers, b"".join(chunks)


async def _chat(body: dict):
    """跑一趟 chat_completions 并把响应读完。"""
    response = await gateway.chat_completions(_request(body))
    return await _read(response)


# ---------------------------------------------------------------------------
# ① 3000100：识别 + 降级重试一次 + 告知
# ---------------------------------------------------------------------------
def test_classify_3000100_is_max_input_tokens():
    kind = marvis_client._classify(
        400, '{"code":3000100,"msg":"max input token error"}')
    assert kind == "max_input_tokens"


def test_classify_5100007_is_upstream_internal():
    kind = marvis_client._classify(
        500, '{"code":5100007,"msg":"服务端通用错误:服务端严重报错"}')
    assert kind == "upstream_internal"


def test_max_tokens_degrade_retries_once_with_32768(monkeypatch):
    """max_tokens=128000 撞 3000100 -> 自动降到 32768 再打一次并记录。"""
    seen = []

    def fake_stream(_data, _realm, body, account_id=None):
        seen.append(body.get("max_tokens"))
        if body.get("max_tokens") > gateway.MAX_TOKENS_DEGRADE_THRESHOLD:
            raise _max_input_error()
        return _FakeStream()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)
    notes: dict = {}
    token = gateway._REQUEST_NOTES.set(notes)
    try:
        result = gateway._call_upstream(
            {}, "cn", {"model": "main-auto", "max_tokens": 128000}, 192,
            stream=True)
    finally:
        gateway._REQUEST_NOTES.reset(token)

    assert isinstance(result, _FakeStream)
    assert seen == [128000, gateway.MAX_TOKENS_DEGRADE_TARGET]
    assert notes["degraded_max_tokens"] == {
        "from": 128000, "to": gateway.MAX_TOKENS_DEGRADE_TARGET}


def test_max_tokens_small_is_not_downgraded(monkeypatch):
    calls = []

    def fake_stream(_d, _r, body, account_id=None):
        calls.append(body.get("max_tokens"))
        raise _max_input_error()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)
    with pytest.raises(marvis_client.UpstreamError):
        gateway._call_upstream({}, "cn", {"model": "m", "max_tokens": 4096}, 1,
                               stream=True)
    assert calls == [4096], "低于阈值的 max_tokens 不许被悄悄改"


def test_max_tokens_degrade_is_visible_to_caller():
    """①c：降级必须**告知** —— 响应头带请求值/实际值。"""
    notes = {"degraded_max_tokens": {"from": 128000, "to": 32768}}
    headers = gateway._degraded_headers(notes)
    assert headers["X-Marvis-Max-Tokens-Degraded"] == "32768"
    assert headers["X-Marvis-Max-Tokens-Requested"] == "128000"
    assert gateway._degraded_headers({}) == {}


def test_fail_message_explains_account_model_quota():
    """文案口径：**该账号在该模型上的当次额度不足**。

    旧的「输入 token + max_tokens 合计一条预算」已被 25 发实测证伪，
    文案里不许再出现；也不许把人引向「减上下文」（体积维度已否证）。
    """
    resp = gateway._fail(_max_input_error(), stream=False)
    payload = json.loads(resp.body.decode())
    assert resp.status_code == 400
    assert payload["error"]["code"] == "max_input_tokens_exceeded"
    assert payload["error"]["upstream_code"] == "3000100"
    assert payload["error"]["family"] == "quota"
    text = payload["error"]["message"]
    assert "该账号在该模型上的当次可用额度" in text
    assert "不是请求太大" in text
    assert "API Key" in text
    assert "总额度" in text, "必须说清不是账号池总额度耗尽"
    assert "换一个 Marvis 账号" in text, "要给出可操作建议"
    assert "输入 token + max_tokens" not in text, "被证伪的解释不许再写"
    assert "减少上下文" not in text, "不许再把人引向减上下文"



def test_max_input_after_degrade_switches_account_then_reports(
        monkeypatch, note_calls):
    """降级一次 -> 同号仍 3000100 -> 换号；换号耗尽才报，且带 upstream_code。"""
    accounts = [_FakeAccount(193), _FakeAccount(194)]
    selected, _released = _install_pool(monkeypatch, accounts)
    attempts = []
    marked = []

    def fake_stream(_d, _r, body, account_id=None):
        attempts.append((account_id, body.get("max_tokens")))
        raise _max_input_error()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)
    monkeypatch.setattr(
        gateway, "_note_model_quota_tight",
        lambda db, account, model, *, code="": marked.append(
            (int(account.id), model, code)))

    notes: dict = {}
    token = gateway._REQUEST_NOTES.set(notes)
    try:
        with pytest.raises(marvis_client.UpstreamError) as info:
            gateway._attempt(_FakeDB(), None, None,
                             {"model": "main-auto", "max_tokens": 128000},
                             "cn", stream=True)
    finally:
        gateway._REQUEST_NOTES.reset(token)

    assert attempts == [(193, 128000), (193, 32768), (194, 32768)], (
        "先降级一次（同号），降级后仍失败才换号；换号沿用降级后的形状")
    assert len(selected) == 3, "两个号 + 一次空选（换号耗尽）"
    assert marked == [(193, "main-auto", "3000100"),
                      (194, "main-auto", "3000100")], (
        "每个降级后仍失败的号都要标 (账号×模型) 额度紧张，不是账号级")
    assert notes["degraded_max_tokens"] == {
        "from": 128000, "to": gateway.MAX_TOKENS_DEGRADE_TARGET}
    assert notes["quota_switch_accounts"] == 2, "两个号都试过"
    assert note_calls[-1]["count_error"] is False, (
        "3000100 不是号的错：不许把好号推去熔断")

    resp = gateway._fail(info.value, notes=notes, stream=False)
    payload = json.loads(resp.body.decode())
    assert resp.status_code == 400, "换号耗尽才把 3000100 返回给用户"
    assert payload["error"]["upstream_code"] == "3000100"
    assert payload["error"]["family"] == "quota"
    assert "试过 2 个账号" in payload["error"]["message"]


def test_failure_profile_logs_max_tokens_and_degraded(monkeypatch, caplog):
    """失败画像必须带 max_tokens（原始值）与 degraded，否则事后无法判定。"""
    def fake_stream(*_a, **_k):
        raise _max_input_error()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)
    with caplog.at_level("INFO"):
        with pytest.raises(marvis_client.UpstreamError):
            gateway._call_upstream(
                {}, "cn", {"model": "main-auto", "max_tokens": 128000},
                193, stream=True)
    lines = [r.getMessage() for r in caplog.records
             if "失败画像" in r.getMessage()]
    assert len(lines) == 2, "降级前后各一条失败画像"
    assert "max_tokens=128000" in lines[0]
    assert "degraded=0" in lines[0]
    assert "max_tokens=128000" in lines[1], "降级后仍要记**原始**请求值"
    assert "degraded=1" in lines[1]
    assert "attempt=2/3" in lines[1]


def test_1000007_and_3000100_share_quota_family():
    """1000007「积分余额不足」与 3000100 同族，但错误体保留 upstream_code。"""
    assert marvis_client._classify(
        400, '{"code":3000100,"msg":"max input token error"}') == (
            "max_input_tokens")
    assert marvis_client._classify(
        400, '{"code":1000007,"msg":"积分余额不足，请获取积分后再试"}') == (
            "quota_exceeded")

    resp_a = gateway._fail(_max_input_error(), stream=False)
    resp_b = gateway._fail(
        _err("1000007", 400, "quota_exceeded", "积分余额不足，请获取积分后再试"),
        stream=False)
    a = json.loads(resp_a.body.decode())["error"]
    b = json.loads(resp_b.body.decode())["error"]

    assert a["family"] == b["family"] == "quota"
    assert a["upstream_code"] == "3000100", "上游原始码必须保留"
    assert b["upstream_code"] == "1000007"
    assert a["code"] != b["code"], "同族但仍要能区分具体原因"
    for payload in (a, b):
        assert "该账号在该模型上的当次可用额度" in payload["message"]
        assert "不是请求太大" in payload["message"]
        assert "输入 token + max_tokens" not in payload["message"]

# ---------------------------------------------------------------------------
# ② 5100007：同号重试一次，不换号、不计账
# ---------------------------------------------------------------------------
def test_internal_error_retries_same_account_only(monkeypatch):
    """核心断言：3 个号的池子，5100007 只许打在**第一个号**上两次。"""
    accounts = [_FakeAccount(192), _FakeAccount(191), _FakeAccount(194)]
    selected, _released = _install_pool(monkeypatch, accounts)
    attempts = []

    def fake_stream(_d, _r, body, account_id=None):
        attempts.append(account_id)
        raise _internal_error()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)

    started = time.perf_counter()
    with pytest.raises(marvis_client.UpstreamError) as info:
        gateway._attempt(_FakeDB(), None, None,
                         {"model": "main-auto"}, "cn", stream=True)
    elapsed = time.perf_counter() - started

    assert info.value.kind == "upstream_internal"
    assert attempts == [192, 192], "同号两次：首发 + 1 次重试，绝不换号"
    assert len(selected) == 1, "只选过一次号（没有任何换号动作）"
    assert elapsed >= gateway.INTERNAL_RETRY_BASE, "必须有 1~2s 退避"


def test_internal_error_does_not_count_or_cool(monkeypatch, note_calls):
    account = _FakeAccount(192)
    other = _FakeAccount(191)
    _install_pool(monkeypatch, [account, other])
    monkeypatch.setattr(
        gateway.client, "open_chat_stream",
        lambda *a, **k: (_ for _ in ()).throw(_internal_error()))

    before = account.err_count
    with pytest.raises(marvis_client.UpstreamError):
        gateway._attempt(_FakeDB(), account, None, {"model": "m"}, "cn",
                         stream=True)

    assert note_calls, "失败仍要留痕（可观测性）"
    last = note_calls[-1]
    assert last.get("count_error") is False, "5100007 不许计入 err_count"
    assert last.get("cool_seconds") in (0, None), "5100007 不许打冷却"
    assert account.err_count == before
    assert account.cool_until is None


def test_note_failure_count_error_false_skips_err_count():
    account = _FakeAccount(7)
    shared.note_failure(_FakeDB(), account, kind="upstream_internal",
                        detail="5100007", status=500, count_error=False)
    assert account.err_count == 0
    shared.note_failure(_FakeDB(), account, kind="upstream_error",
                        detail="boom", status=500)
    assert account.err_count == 1


# ---------------------------------------------------------------------------
# ③ acquire 超时
# ---------------------------------------------------------------------------
def test_acquire_timeout_env_default_and_override(monkeypatch):
    assert gateway._acquire_timeout() == gateway.ACQUIRE_TIMEOUT_DEFAULT
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "5")
    assert gateway._acquire_timeout() == 5.0
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "abc")
    assert gateway._acquire_timeout() == gateway.ACQUIRE_TIMEOUT_DEFAULT
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "0")
    assert gateway._acquire_timeout() == 0.0


def _slow_acquire(monkeypatch, delay: float, released: list):
    def fake_acquire(request, body, realm, *, stream):
        time.sleep(delay)
        return None, _FakeStream(), _FakeAccount(192), time.perf_counter()

    monkeypatch.setattr(gateway, "_acquire", fake_acquire)
    monkeypatch.setattr(gateway.shared, "release",
                        lambda account: released.append(
                            int(getattr(account, "id", 0) or 0)))


def _timed_chat(body: dict, settle: float = 0.9):
    """在**协程内部**计时，并在返回前让事件循环活着等 done callback。

    asyncio.run 结束时还要 shutdown 默认线程池，那段会等假上游跑完，会把测量
    污染成上游的耗时；回收又是 done callback，循环一关就没人跑。
    """
    async def _run():
        started = time.perf_counter()
        out = await _chat(body)
        elapsed = time.perf_counter() - started
        await asyncio.sleep(settle)
        return out, elapsed

    return asyncio.run(_run())


def test_acquire_timeout_within_grace_returns_real_status(monkeypatch):
    """③ + 混合策略：**宽限期内**超时 —— 还没写字节，必须给真实 504。

    上一版把这条也包成 200 + 错误帧，只看 HTTP 状态的客户端会把失败当成功。
    """
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "0.2")
    monkeypatch.setattr(gateway, "_validate_model", lambda body: "main-auto")
    released = []
    _slow_acquire(monkeypatch, 0.6, released)

    (status, headers, body), elapsed = _timed_chat(
        {"model": "main-auto", "stream": True})

    assert elapsed < 0.5, "必须在超时上限内收尾，不能等上游"
    assert status == 504, "宽限期内失败：状态码是真的，不许包成 200"
    assert headers.get("retry-after") == "5"
    assert "x-marvis-stream-error" not in headers
    payload = json.loads(body.decode())
    assert payload["error"]["code"] == "acquire_timeout"
    assert payload["error"]["http_status"] == 504
    assert "prefill" in payload["error"]["message"]

    # 迟到的上游响应必须被回收（否则那个号的租约永远不还）
    assert released == [192]


def test_acquire_timeout_after_grace_returns_error_frame(monkeypatch):
    """③ + 混合策略：**超过宽限期**才失败 —— 头已发，只能 200 + 错误帧。

    这一条锁住「慢失败」（prefill 卡死）时的对外形状：首帧心跳 → 错误帧 →
    [DONE]，原始状态放在帧的 http_status 里。
    """
    monkeypatch.setenv(gateway.STREAM_STATUS_GRACE_ENV, "0.2")
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "0.5")
    monkeypatch.setattr(gateway, "_validate_model", lambda body: "main-auto")
    released = []
    _slow_acquire(monkeypatch, 0.7, released)

    (status, headers, body), elapsed = _timed_chat(
        {"model": "main-auto", "stream": True})

    assert elapsed < 0.9, "宽限期 + 剩下的等待必须在超时上限附近收尾"
    assert status == 200, "流已经开始：状态由帧携带"
    assert headers.get("x-marvis-stream-pending") == "1"
    assert body.startswith(b'data: {"id":"chatcmpl-keepalive"'), \
        "首帧必须是心跳（headers-first 没退化）"
    assert b'"code": "acquire_timeout"' in body
    assert b'"http_status": 504' in body
    assert body.endswith(b"data: [DONE]\n\n")
    assert released == [192]


def test_stream_status_grace_env_default_and_override(monkeypatch):
    """③b：宽限期必须可配（默认 0.8s，理由见 gateway 里的注释）。"""
    assert gateway._stream_status_grace() == gateway.STREAM_STATUS_GRACE_DEFAULT
    monkeypatch.setenv(gateway.STREAM_STATUS_GRACE_ENV, "1.5")
    assert gateway._stream_status_grace() == 1.5
    monkeypatch.setenv(gateway.STREAM_STATUS_GRACE_ENV, "abc")
    assert gateway._stream_status_grace() == gateway.STREAM_STATUS_GRACE_DEFAULT
    monkeypatch.setenv(gateway.STREAM_STATUS_GRACE_ENV, "0")
    assert gateway._stream_status_grace() == 0.0


def test_acquire_timeout_non_stream_is_504(monkeypatch):
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "0.2")
    monkeypatch.setattr(gateway, "_validate_model", lambda body: "main-auto")
    _slow_acquire(monkeypatch, 0.5, [])

    status, headers, body = asyncio.run(_chat({"model": "main-auto"}))
    assert status == 504
    assert headers.get("retry-after") == "5"
    payload = json.loads(body.decode())
    assert payload["error"]["code"] == "acquire_timeout"
    assert payload["error"]["http_status"] == 504


# ---------------------------------------------------------------------------
# ④ 错误帧格式
# ---------------------------------------------------------------------------
def test_error_frame_exact_bytes_and_headers():
    resp = gateway._stream_error_response(
        500, {"type": "upstream", "code": "upstream_internal_error",
              "message": "boom"})
    status, headers, body = asyncio.run(_read(resp))

    assert status == 200
    assert headers.get("content-type", "").startswith("text/event-stream")
    assert headers.get("x-marvis-stream-error") == "1"
    assert headers.get("x-marvis-stream-error-status") == "500"
    chunks = body.split(b"\n\n")
    assert chunks[0].startswith(b'data: {"error":')
    assert json.loads(chunks[0][6:])["error"]["http_status"] == 500
    assert chunks[1] == b"data: [DONE]"
    assert body.endswith(b"\n\n")


def test_fail_on_stream_is_200_with_error_frame():
    resp = gateway._fail(_internal_error(), stream=True)
    status, headers, body = asyncio.run(_read(resp))
    assert status == 200
    assert headers.get("x-marvis-stream-error") == "1"
    payload = json.loads(body.split(b"data:", 1)[1].split(b"\n", 1)[0])
    assert payload["error"]["code"] == "upstream_internal_error"
    assert payload["error"]["http_status"] == 502
    assert b"data: [DONE]" in body


def test_bridge_protocols_keep_real_status(monkeypatch):
    """/v1/messages 与 /v1/responses 不能拿到 200 + 错误帧（中继认不出）。"""
    async def fake_chat(_inner):
        return gateway._stream_error_response(
            502, {"type": "upstream", "code": "upstream_internal_error",
                  "message": "boom"})

    monkeypatch.setattr(gateway, "chat_completions", fake_chat)
    resp = asyncio.run(gateway._run_normalized(
        _request({"model": "m"}), {"model": "m", "stream": True},
        protocol="anthropic"))
    assert resp.status_code == 502
    payload = json.loads(resp.body.decode())
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "upstream"


# ---------------------------------------------------------------------------
# ⑤ 超大请求：不拒绝，但要把日志提到 ERROR
# ---------------------------------------------------------------------------
def test_huge_request_logs_error_with_context(monkeypatch, caplog):
    big = "y" * (gateway.HUGE_REQUEST_BYTES + 1024)

    def fake_stream(_d, _r, _body, account_id=None):
        return _FakeStream()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)
    with caplog.at_level("DEBUG"):
        gateway._call_upstream(
            {}, "cn",
            {"model": "main-auto",
             "messages": [{"role": "user", "content": big}]},
            192, stream=True)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, ">4MB 必须是 ERROR 级"
    text = errors[0].getMessage()
    assert "超大请求" in text
    assert "acc=192" in text
    assert "msgs=1" in text


def test_request_just_under_threshold_stays_info(monkeypatch, caplog):
    small = "y" * (2 * 1024 * 1024)

    def fake_stream(_d, _r, _body, account_id=None):
        return _FakeStream()

    monkeypatch.setattr(gateway.client, "open_chat_stream", fake_stream)
    with caplog.at_level("DEBUG"):
        gateway._call_upstream(
            {}, "cn",
            {"model": "main-auto",
             "messages": [{"role": "user", "content": small}]},
            192, stream=True)
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_5mb_request_is_not_rejected():
    """⑤a：保持不拒绝 —— 413 只在超过网关兜底上限（默认 256 MiB）时出现。"""
    assert gateway._max_body_bytes() == gateway.DEFAULT_MAX_BODY_BYTES
    assert 5 * 1024 * 1024 < gateway.DEFAULT_MAX_BODY_BYTES
