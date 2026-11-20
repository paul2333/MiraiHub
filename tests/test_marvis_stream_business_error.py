#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式分支：上游业务错误码必须透传到客户端错误帧（PR-21）。

## 现象（2026-11-20 线上实测）

3.32 MB / 180 条消息的流式请求，上游回业务错误
    {"code":3000100,"msg":"max input token error"}
（或 3000101 invalid parameter），服务端日志是：

    Marvis 流中断 acc=0: TypeError: cannot unpack non-iterable NoneType object

客户端只收到一帧 stream_broken、0 正文字符 —— 真实原因（token 超限 / 参数被
拒）完全丢失，用户以为「上游断开了」，于是无脑重试。

## 根因

流式生成器在 acquire 失败分支里把 _error_payload() 当**三元组**解包：

    status, error_obj, _hdr = _error_payload(exc, notes)

而 _error_payload() 的签名是 tuple | None：它只接管 max_input_tokens /
upstream_internal / quota_exceeded 三种 kind，其余一律返回 None —— 这是刻意
的（_fail() 里紧跟着 if quota is not None: 的 None 判定）。业务码 3000101 归
upstream_business；3000100 走「200 + application/json 业务错误体」时归
http_error —— 两者都返回 None，解包当场抛 TypeError，被外层
except Exception 兜成 stream_broken。acc=0 正是这条路径的指纹：account 还没
被赋值。

## 本文件锁住什么

① 业务码必须出现在错误帧里（upstream_code 与 message 都带），绝不允许退化成
   stream_broken；
② 额度族（3000100）与 _error_payload() 的口径一致：family=quota、
   code=max_input_tokens_exceeded —— 不管它是以 400 还是「200 + JSON」到达，
   业务码才是判据；
③ **真的**读不出错误（非业务失败）才退化 stream_broken，且 detail 里要带截断
   后的原始字节片段（≤200 字符），否则排障只剩「断开」两个字；
④ 流内裸错误帧（没有 {"error":...} 信封，只有 code + msg）同样要透传业务码；
⑤ 正常流一个字节都不许改（对照组）。

运行：
  python -m pytest tests/test_marvis_stream_business_error.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from admin.platform.marvis import gateway  # noqa: E402
from admin.platform.marvis.core import client  # noqa: E402


# ---------------------------------------------------------------------------
# 最小 ASGI 请求 / 响应读取（与 test_marvis_failure_hardening.py 同款：
# 只读字节与头部，不依赖 Starlette 版本差异）
# ---------------------------------------------------------------------------
def _scope(path: str = "/v1/chat/completions") -> dict:
    return {"type": "http", "method": "POST", "path": path,
            "headers": [(b"content-type", b"application/json")],
            "query_string": b""}


def _request(body: dict):
    from starlette.requests import Request

    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(_scope(), receive)


async def _read(response):
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
    response = await gateway.chat_completions(_request(body))
    return await _read(response)


def _frames(body: bytes) -> list:
    """把 SSE 字节流切成 data 载荷列表（跳过 [DONE]）。"""
    out = []
    for line in body.split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        out.append(json.loads(payload))
    return out


def _error_frames(body: bytes) -> list:
    return [f["error"] for f in _frames(body) if isinstance(f.get("error"), dict)]


# ---------------------------------------------------------------------------
# 假上游 / 假账号
# ---------------------------------------------------------------------------
class _Account:
    id = 192
    status = "active"


class _Resp:
    """按顺序吐块，吐完 EOF（让真 _iter_upstream 跑起来）。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read1(self, _n=-1):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        pass


async def _noop_bill(*_a, **_kw):
    return None


@pytest.fixture()
def quiet(monkeypatch):
    """把记账 / 成功标记 / 释放都打成 no-op（本文件不验这几件事）。"""
    monkeypatch.setattr(gateway, "_validate_model", lambda body: "main-auto")
    monkeypatch.setattr(gateway, "_bill_async", _noop_bill)
    monkeypatch.setattr(gateway.shared, "note_success", lambda account: None)
    monkeypatch.setattr(gateway.shared, "release", lambda account: None)
    # 宽限期拉到最短、acquire 上限给足 —— 稳定命中「头已发、只能补帧」那条路
    monkeypatch.setenv(gateway.STREAM_STATUS_GRACE_ENV, "0.05")
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "5")
    return monkeypatch


def _fail_after_grace(monkeypatch, exc: BaseException, delay: float = 0.2):
    """acquire 在宽限期**之后**失败：响应头已发，失败只能靠错误帧携带。"""

    def fake_acquire(request, body, realm, *, stream):
        time.sleep(delay)
        raise exc

    monkeypatch.setattr(gateway, "_acquire", fake_acquire)


def _json_body_error(code: int, msg: str) -> client.UpstreamError:
    """上游「200 + application/json 业务错误体」的形状（_reject_json_body 抛的）。

    kind 由 client._classify_json_body() 给出：3000100 / 3000101 都不在它的
    白名单里，一律 http_error —— 这正是线上 3.32 MB 那条请求的形态。
    """
    payload = json.dumps({"code": code, "msg": msg})
    kind = client._classify_json_body(payload)
    return client.UpstreamError(
        "模型网关没有返回 SSE 流，而是回了一段 JSON（上游业务错误）：%s" % payload,
        status=502, code=kind, kind=kind, detail=payload)


# ---------------------------------------------------------------------------
# ① 3000100（200 + JSON 业务错误体）：线上 T4 的确切形状
# ---------------------------------------------------------------------------
def test_max_input_token_error_after_grace_keeps_code(quiet):
    exc = _json_body_error(3000100, "max input token error")
    assert exc.kind == "http_error", (
        "口径前提变了：3000100 走 200+JSON 时归 http_error，"
        "这条路径以前会 unpack None")
    _fail_after_grace(quiet, exc)

    status, headers, body = asyncio.run(
        _chat({"model": "main-auto", "stream": True,
               "messages": [{"role": "user", "content": "hi"}]}))

    assert status == 200, "流已开始：状态由帧携带"
    assert headers.get("x-marvis-stream-pending") == "1"
    assert b"stream_broken" not in body, "业务失败不许伪装成上游断开"
    text = body.decode("utf-8", "replace")
    assert "TypeError" not in text and "cannot unpack" not in text, text[:400]

    errors = _error_frames(body)
    assert len(errors) == 1, "错误帧必须恰好一帧（后面跟 [DONE]）"
    err = errors[0]
    assert err.get("upstream_code") == "3000100", err
    assert err.get("family") == "quota", err
    assert err.get("code") == "max_input_tokens_exceeded", err
    assert "3000100" in err.get("message", ""), err
    assert "max input token error" in err.get("message", ""), err
    assert err.get("http_status") == 400, err
    assert body.endswith(b"data: [DONE]\n\n")


# ---------------------------------------------------------------------------
# ② 3000101（400 参数错）：归 upstream_business，同样不许吞
# ---------------------------------------------------------------------------
def test_invalid_parameter_3000101_after_grace_keeps_code(quiet):
    detail = json.dumps({"code": 3000101, "msg": "invalid parameter"})
    _fail_after_grace(quiet, client.UpstreamError(
        "模型网关失败（HTTP 400）", status=400, code="400",
        kind="upstream_business", detail=detail))

    status, _headers, body = asyncio.run(
        _chat({"model": "main-auto", "stream": True}))

    assert status == 200
    assert b"stream_broken" not in body
    errors = _error_frames(body)
    assert len(errors) == 1, body[:400]
    err = errors[0]
    assert err.get("upstream_code") == "3000101", err
    assert "3000101" in err.get("message", ""), err
    assert "invalid parameter" in err.get("message", ""), err


# ---------------------------------------------------------------------------
# ③ 真的读不出错误：才退化 stream_broken，但要带原始片段
# ---------------------------------------------------------------------------
def test_genuine_stream_break_degrades_with_raw_snippet(quiet, monkeypatch):
    """非业务失败（连接被重置）仍然 stream_broken —— 但要说清断在哪。"""
    marker = "upstream-tail-marker"

    async def fake_iter(_resp):
        yield ('data: {"choices":[{"delta":{"content":"%s"}}]}\n\n'
               % marker).encode()
        raise ConnectionResetError("peer reset by upstream")

    monkeypatch.setattr(gateway, "_acquire", lambda request, body, realm, *,
                        stream: (None, _Resp([]), _Account(), 0.0))
    monkeypatch.setattr(gateway, "_iter_upstream", fake_iter)

    status, _headers, body = asyncio.run(
        _chat({"model": "main-auto", "stream": True}))

    assert status == 200
    errors = _error_frames(body)
    assert len(errors) == 1, "断流也要有一帧说明"
    err = errors[0]
    assert err.get("code") == "stream_broken", err
    assert marker in err.get("message", ""), (
        "退化时必须带上原始字节片段，否则排障只剩「断开」两个字")
    assert "ConnectionResetError" in err.get("message", ""), err
    assert body.endswith(b"data: [DONE]\n\n")


# ---------------------------------------------------------------------------
# ④ 流内裸错误帧（无 {"error":...} 信封）
# ---------------------------------------------------------------------------
def test_inband_bare_code_frame_keeps_code(quiet, monkeypatch):
    monkeypatch.setattr(gateway, "_acquire", lambda request, body, realm, *,
                        stream: (None, _Resp([
                            b'data: {"code":3000100,"msg":"max input token error"}\n\n',
                        ]), _Account(), 0.0))

    status, _headers, body = asyncio.run(
        _chat({"model": "main-auto", "stream": True}))

    assert status == 200
    errors = _error_frames(body)
    assert len(errors) == 1, body[:400]
    assert errors[0].get("upstream_code") == "3000100", errors[0]
    assert "3000100" in errors[0].get("message", ""), errors[0]


# ---------------------------------------------------------------------------
# ⑤ 对照组：正常流一个字节都不改
# ---------------------------------------------------------------------------
def test_normal_stream_is_untouched(quiet, monkeypatch):
    frames = [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
              b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
              b'"usage":{"total_tokens":3}}\n\n',
              b"data: [DONE]\n\n"]
    monkeypatch.setattr(gateway, "_acquire", lambda request, body, realm, *,
                        stream: (None, _Resp(list(frames)), _Account(), 0.0))

    status, _headers, body = asyncio.run(
        _chat({"model": "main-auto", "stream": True}))

    assert status == 200
    assert body.startswith(frames[0])
    assert b'"content":"hi"' in body
    assert b'"total_tokens":3' in body
    assert _error_frames(body) == [], "正常流不许插错误帧"
    assert body.count(b"data: [DONE]") == 1, "终止帧不许重复"
