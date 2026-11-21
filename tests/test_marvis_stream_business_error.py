"""Persistent regression tests for business errors and termination of Marvis SSE."""
import asyncio
import json
import threading

import pytest
from starlette.requests import Request

from admin.platform.marvis import gateway
from admin.platform.marvis.core import client


class Account:
    id = 192


class Response:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = 0

    def read1(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed += 1


@pytest.fixture
def quiet(monkeypatch):
    calls = {"success": [], "release": [], "bill": []}

    async def bill(*args, **kwargs):
        calls["bill"].append(kwargs)

    monkeypatch.setattr(gateway, "_validate_model", lambda body: "main-auto")
    monkeypatch.setattr(gateway, "_bill_async", bill)
    monkeypatch.setattr(gateway.shared, "note_success", calls["success"].append)
    monkeypatch.setattr(gateway.shared, "release", calls["release"].append)
    monkeypatch.setattr(client, "note_sentinel", lambda *args, **kwargs: None)
    monkeypatch.setenv(gateway.STREAM_STATUS_GRACE_ENV, "0.01")
    monkeypatch.setenv(gateway.ACQUIRE_TIMEOUT_ENV, "5")
    monkeypatch.setenv(gateway._KEEPALIVE_ENV, "0")
    return calls


def request(*, stream=True):
    async def receive():
        return {"type": "http.request", "body": json.dumps({
            "model": "main-auto", "stream": stream,
            "messages": [{"role": "user", "content": "hello"}],
        }).encode(), "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "headers": [], "query_string": b""}, receive)


async def chat(*, release=None, stream=True):
    response = await gateway.chat_completions(request(stream=stream))
    if release is not None:
        release.set()
    if hasattr(response, "body_iterator"):
        body = b"".join([chunk async for chunk in response.body_iterator])
    else:
        body = response.body
    return response.status_code, body


def errors(body):
    result = []
    for line in body.splitlines():
        if line.startswith(b"data:") and line[5:].strip() != b"[DONE]":
            value = json.loads(line[5:])
            if isinstance(value.get("error"), dict):
                result.append(value["error"])
    return result


@pytest.mark.parametrize("code,kind,status", [
    (3000100, "http_error", 502), (3000101, "upstream_business", 400),
    (4100403, "auth", 401), (4100404, "risk_control", 403),
])
@pytest.mark.parametrize("delayed", [False, True])
def test_acquire_business_error_keeps_classification(quiet, monkeypatch, code, kind, status, delayed):
    release = threading.Event()
    exc = client.UpstreamError("upstream rejected", status=status, code=str(status),
                               kind=kind, detail=json.dumps({"code": code, "msg": "rejected"}))

    def acquire(*args, **kwargs):
        if delayed:
            assert release.wait(3)
        raise exc

    monkeypatch.setattr(gateway, "_acquire", acquire)
    http, body = asyncio.run(chat(release=release if delayed else None))
    error = errors(body)[0] if delayed else json.loads(body)["error"]
    assert error["upstream_code"] == str(code)
    assert error["code"] != "stream_broken"
    if code == 3000100:
        assert error["family"] == "quota"
        assert error["code"] == "max_input_tokens_exceeded"
        assert error.get("http_status", http) == 400
    if delayed:
        assert http == 200 and len(errors(body)) == 1
        assert body.count(b"data: [DONE]") == 1
    assert not quiet["success"]


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("ending", [b"", b"data: [DONE]", b"data: [DONE]\n\n"])
def test_inband_error_is_normalized_once_and_billed_as_failure(quiet, monkeypatch, envelope, ending):
    error = {"code": 3000100, "msg": "max input token error"}
    value = {"error": error, "choices": []} if envelope else error
    frame = b"data: " + json.dumps(value).encode() + b"\n\n"
    response = Response([frame[:12], frame[12:], ending[:7], ending[7:]])
    # Empty chunks represent EOF, so omit them from the synthetic transport.
    response.chunks = [chunk for chunk in response.chunks if chunk]
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
    http, body = asyncio.run(chat())
    assert http == 200
    assert len(errors(body)) == 1
    assert errors(body)[0]["upstream_code"] == "3000100"
    assert body.count(b"data: [DONE]") == 1
    assert body.endswith(b"data: [DONE]\n\n")
    assert not quiet["success"]
    assert quiet["bill"][0]["error"]
    assert response.closed == 1 and len(quiet["release"]) == 1


def test_midstream_business_exception_keeps_code(quiet, monkeypatch):
    async def source(_response):
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        raise client.UpstreamError("upstream rejected", status=400, kind="upstream_business",
                                   detail='{"code":3000101,"msg":"invalid parameter"}')

    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, Response([]), Account(), 0.0))
    monkeypatch.setattr(gateway, "_iter_upstream", source)
    _, body = asyncio.run(chat())
    assert errors(body)[0]["upstream_code"] == "3000101"
    assert body.count(b"data: [DONE]") == 1
    assert not quiet["success"] and quiet["bill"][0]["error"]


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_content_before_error_is_preserved_for_sse_line_endings(quiet, monkeypatch, newline):
    prior = b'data: {"choices":[{"delta":{"content":"keep this text"}}]}' + newline * 2
    failure = b'data: {"code":3000101,"msg":"invalid parameter"}' + newline * 2
    response = Response([prior + failure + b"data: [DONE]" + newline * 2])
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
    _, body = asyncio.run(chat())
    assert body.startswith(prior)
    assert len(errors(body)) == 1 and errors(body)[0]["upstream_code"] == "3000101"
    assert body.count(b"data: [DONE]") == 1


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_normal_line_endings_survive_every_transport_split(quiet, monkeypatch, newline):
    body = b'data: {"choices":[{"delta":{"content":"hi"}}]}' + newline * 2
    body += b"data: [DONE]" + newline * 2

    async def verify():
        for split in range(1, len(body)):
            response = Response([body[:split], body[split:]])
            monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
            _, actual = await chat()
            assert actual == body, split
    asyncio.run(verify())


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("ending", [b"\n\n", b""])
def test_oversized_error_event_remains_parseable_after_partial_forwarding(quiet, monkeypatch, envelope, ending):
    error = {"code": 3000101, "msg": "x" * (gateway._FRAME_MAX + 65536 + 128)}
    value = {"error": error} if envelope else error
    frame = b"data: " + json.dumps(value).encode() + ending
    response = Response([frame[i:i + 65536] for i in range(0, len(frame), 65536)])
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
    _, body = asyncio.run(chat())
    # Once a large event prefix has streamed out, its remaining bytes cannot
    # be replaced. Preserve the valid event and provide one terminal marker.
    assert body.startswith(frame)
    error_frames = errors(body)
    assert len(error_frames) == 1
    assert str(error_frames[0].get("upstream_code") or error_frames[0]["code"]) == "3000101"
    assert body.count(b"data: [DONE]") == 1
    assert not quiet["success"] and quiet["bill"][0]["error"]


def test_oversized_crlf_content_keeps_final_lf_before_error(quiet, monkeypatch):
    prior = b'data: {"choices":[{"delta":{"content":"' + b"x" * gateway._FRAME_MAX
    prior += b'"}}]}\r\n\r\n'
    failure = b'data: {"code":3000101,"msg":"invalid parameter"}\r\n\r\n'
    response = Response([prior[:-1], prior[-1:] + failure])
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
    _, body = asyncio.run(chat())
    assert body.startswith(prior)
    assert len(errors(body)) == 1


@pytest.mark.parametrize("envelope", [False, True])
def test_oversized_error_data_line_and_blank_line_split_are_distinct_events(quiet, monkeypatch, envelope):
    error = {"code": 3000101, "msg": ""}
    value = {"error": error} if envelope else error
    overhead = len(b"data: " + json.dumps(value).encode() + b"\n")
    error["msg"] = "x" * (655360 - overhead)
    line = b"data: " + json.dumps(value).encode() + b"\n"
    assert len(line) == 655360
    chunks = [line[i:i + 65536] for i in range(0, len(line), 65536)]
    response = Response(chunks + [b"\ndata: [DONE]\n\n"])
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
    _, body = asyncio.run(chat())
    payloads = []
    for event in body.split(b"\n\n"):
        data = b"\n".join(line[5:].strip() for line in event.splitlines() if line.startswith(b"data:"))
        if data:
            payloads.append(data)
            if data != b"[DONE]":
                json.loads(data)  # Multiple data fields form ONE payload, per SSE.
    assert payloads[-1] == b"[DONE]"
    assert payloads.count(b"[DONE]") == 1


def test_connection_reset_does_not_echo_exception_or_stream_tail(quiet, monkeypatch):
    marker = "private-model-content"

    async def source(_response):
        yield ('data: {"choices":[{"delta":{"content":"%s"}}]}\n\n' % marker).encode()
        raise ConnectionResetError("private-upstream-address-and-token")

    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, Response([]), Account(), 0.0))
    monkeypatch.setattr(gateway, "_iter_upstream", source)
    _, body = asyncio.run(chat())
    error = errors(body)[0]
    assert error["code"] == "stream_broken"
    assert marker not in json.dumps(error)
    assert b"private-upstream-address-and-token" not in body
    assert body.count(b"data: [DONE]") == 1


@pytest.mark.parametrize("success_code", [None, 0, "0", False])
def test_normal_stream_is_byte_for_byte_unchanged(quiet, monkeypatch, success_code):
    value = {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}],
             "usage": {"total_tokens": 3}}
    if success_code is not None:
        value["code"] = success_code
    frame = b"data: " + json.dumps(value).encode() + b"\n\n"
    body = frame + b'data: {"choices":[],"usage":{"total_tokens":3}}\n\n' + b"data: [DONE]\n\n"
    response = Response([body[:15], body[15:]])
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))
    _, actual = asyncio.run(chat())
    assert actual == body
    assert len(quiet["success"]) == 1 and quiet["bill"][0]["error"] == ""


@pytest.mark.parametrize("keepalive", ["0", "1"])
@pytest.mark.parametrize("relay", [None, "anthropic", "responses"])
def test_unstarted_stream_releases_preacquired_connection(quiet, monkeypatch, keepalive, relay):
    from admin.platform import protocol_bridge as bridge

    response = Response([])
    monkeypatch.setenv(gateway._KEEPALIVE_ENV, keepalive)
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, response, Account(), 0.0))

    async def close():
        result = await gateway.chat_completions(request())
        if relay is not None:
            factory = bridge.anthropic_stream_relay if relay == "anthropic" else bridge.responses_stream_relay
            result = factory(result, "m")
            await result.body_iterator.__anext__()  # relay's initial event, before reading upstream
        await result.body_iterator.aclose()
        await result.body_iterator.aclose()

    asyncio.run(close())
    assert response.closed == 1 and len(quiet["release"]) == 1
    assert not quiet["success"]
    assert len(quiet["bill"]) == 1 and quiet["bill"][0]["error"]


def test_unstarted_stream_reaps_late_acquire_without_losing_lease(quiet, monkeypatch):
    release = threading.Event()
    response = Response([])

    def acquire(*args, **kwargs):
        assert release.wait(3)
        return None, response, Account(), 0.0

    monkeypatch.setattr(gateway, "_acquire", acquire)

    async def close():
        try:
            result = await gateway.chat_completions(request())
            await result.body_iterator.aclose()
        finally:
            release.set()
        async with asyncio.timeout(2):
            while not quiet["release"]:
                await asyncio.sleep(0)

    asyncio.run(close())
    assert response.closed == 1 and len(quiet["release"]) == 1
    assert not quiet["success"]


@pytest.mark.parametrize("keepalive", ["0", "1"])
def test_response_start_failure_releases_unread_upstream(quiet, monkeypatch, keepalive):
    from starlette.requests import ClientDisconnect

    upstream = Response([])
    monkeypatch.setenv(gateway._KEEPALIVE_ENV, keepalive)
    monkeypatch.setattr(gateway, "_acquire", lambda *a, **k: (None, upstream, Account(), 0.0))

    async def fail():
        response = await gateway.chat_completions(request())

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            raise OSError("synthetic client disconnected before response start")

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    asyncio.run(fail())
    assert upstream.closed == 1 and len(quiet["release"]) == 1
    assert not quiet["success"] and quiet["bill"][0]["error"]


def test_close_after_heartbeat_reaps_acquire_that_has_just_completed(quiet, monkeypatch):
    release = threading.Event()
    upstream = Response([])
    tasks = []
    ensure_future = asyncio.ensure_future

    def capture(awaitable, *args, **kwargs):
        task = ensure_future(awaitable, *args, **kwargs)
        if getattr(awaitable, "__name__", None) == "to_thread":
            tasks.append(task)
        return task

    def acquire(*args, **kwargs):
        assert release.wait(3)
        return None, upstream, Account(), 0.0

    monkeypatch.setattr(asyncio, "ensure_future", capture)
    monkeypatch.setattr(gateway, "_acquire", acquire)

    async def close():
        try:
            result = await gateway.chat_completions(request())
            await result.body_iterator.__anext__()  # initial heartbeat precedes acquire await
            release.set()
            assert len(tasks) == 1
            await tasks[0]
            await result.body_iterator.aclose()
            await asyncio.sleep(0)
        finally:
            release.set()

    asyncio.run(close())
    assert upstream.closed == 1 and len(quiet["release"]) == 1
    assert not quiet["success"]
