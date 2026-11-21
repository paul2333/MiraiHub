# -*- coding: utf-8 -*-
"""protocol_bridge 流式中继测试：模拟内层 OpenAI chunk SSE，断言三种协议的
工具调用 / 思考块 / 心跳 / 错误事件完整到达客户端。"""
import asyncio
import json
import sys
import pathlib
import time
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "protocol_bridge", str(pathlib.Path(__file__).resolve().parent.parent
                           / "admin" / "platform" / "protocol_bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def frames(*payloads):
    out = []
    for p in payloads:
        if p == ": ping":
            out.append(b": ping\n\n")
        elif p == "[DONE]":
            out.append(b"data: [DONE]\n\n")
        else:
            out.append(("data: %s\n\n" % json.dumps(p, ensure_ascii=False)).encode())
    return out


class FakeInner:
    """模拟内层 StreamingResponse：只提供 body_iterator。"""

    def __init__(self, chunks):
        self.body_iterator = self._gen(chunks)

    async def _gen(self, chunks):
        for c in chunks:
            yield c


def collect(relay_response):
    async def _collect():
        out = []
        async for piece in relay_response.body_iterator:
            if isinstance(piece, bytes):
                out.append(piece.decode())
            else:
                out.append(str(piece))
        return out
    return asyncio.run(_collect())


def parse_events(lines):
    """把 SSE 文本拆成 (type, payload)。

    内层帧可能一段含多行（`event: X\\ndata: Y\\n\\n`），必须按行拆，
    不能假设一段 = 一行。
    """
    events = []
    for piece in lines:
        for line in piece.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith(":"):
                events.append(("comment", line))
                continue
            if line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                events.append((data.get("type") or "?", data))
    return events


TOOL_CHUNKS = frames(
    {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
    {"choices": [{"delta": {"content": "我来读文件"}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "c1", "type": "function",
         "function": {"name": "read", "arguments": "{\"pa"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "th\": \"/a\"}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"usage": {"prompt_tokens": 100, "completion_tokens": 10,
               "total_tokens": 110,
               "prompt_tokens_details": {"cached_tokens": 40}},
     "choices": []},
    "[DONE]",
)


def test_anthropic_relay_tools():
    inner = FakeInner(TOOL_CHUNKS)
    resp = bridge.anthropic_stream_relay(inner, "loomy-x", prefix="t")
    events = parse_events(collect(resp))
    types = [t for t, _ in events]
    assert types[0] == "message_start", types[:3]
    assert "content_block_start" in types
    # 思考块
    thinking_start = [d for t, d in events
                      if t == "content_block_start"
                      and d["content_block"]["type"] == "thinking"]
    assert thinking_start, "缺 thinking 块"
    assert any(d["delta"].get("thinking") == "thinking..."
               for t, d in events if t == "content_block_delta")
    # 正文块
    text_start = [d for t, d in events
                  if t == "content_block_start"
                  and d["content_block"]["type"] == "text"]
    assert text_start
    # 工具块：id+name+完整参数
    tool_start = [d for t, d in events
                  if t == "content_block_start"
                  and d["content_block"]["type"] == "tool_use"]
    assert tool_start and tool_start[0]["content_block"]["id"] == "c1"
    assert tool_start[0]["content_block"]["name"] == "read"
    args = "".join(d["delta"]["partial_json"] for t, d in events
                   if t == "content_block_delta"
                   and d["delta"].get("type") == "input_json_delta")
    assert json.loads(args) == {"path": "/a"}, args
    # 收尾：stop_reason=tool_use + 缓存字段保留
    delta = [d for t, d in events if t == "message_delta"][0]
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["input_tokens"] == 60
    assert delta["usage"]["cache_read_input_tokens"] == 40
    assert types[-1] == "message_stop"
    # 每个开过的块都有 stop
    stops = [d["index"] for t, d in events if t == "content_block_stop"]
    starts = [d["index"] for t, d in events if t == "content_block_start"]
    assert sorted(stops) == sorted(starts), (starts, stops)


def test_responses_relay_tools():
    inner = FakeInner(TOOL_CHUNKS)
    resp = bridge.responses_stream_relay(inner, "loomy-x", prefix="t")
    events = parse_events(collect(resp))
    types = [t for t, _ in events]
    assert types[0] == "response.created"
    args = "".join(d["delta"] for t, d in events
                   if t == "response.function_call_arguments.delta")
    assert json.loads(args) == {"path": "/a"}, args
    fc_done = [d for t, d in events
               if t == "response.output_item.done"
               and d["item"]["type"] == "function_call"]
    assert fc_done and fc_done[0]["item"]["call_id"] == "c1"
    assert fc_done[0]["item"]["arguments"].startswith('{"path"')
    completed = [d for t, d in events if t == "response.completed"][0]
    assert completed["response"]["status"] == "completed"
    assert completed["response"]["usage"]["input_tokens"] == 100
    # output 数组含 function_call 项
    fc_items = [o for o in completed["response"]["output"]
                if o["type"] == "function_call"]
    assert fc_items, "completed.output 缺 function_call"


def test_anthropic_relay_error_event():
    chunks = frames(
        {"choices": [{"delta": {"content": "hi"}}]},
        {"error": {"message": "rate limited", "type": "rate"}},
        "[DONE]",
    )
    resp = bridge.anthropic_stream_relay(FakeInner(chunks), "m", prefix="t")
    text = "".join(collect(resp))
    assert "event: error" in text, "上游错误必须显式发 error 事件"
    assert "rate limited" in text
    assert text.rstrip().endswith('"type": "message_stop"}') or \
        '"message_stop"' in text, "错误后仍要补 message_stop"


def test_responses_relay_incomplete():
    chunks = frames(
        {"choices": [{"delta": {"content": "partial"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        "[DONE]",
    )
    resp = bridge.responses_stream_relay(FakeInner(chunks), "m", prefix="t")
    events = parse_events(collect(resp))
    assert any(t == "response.incomplete" for t, _ in events)
    inc = [d for t, d in events if t == "response.incomplete"][0]
    assert inc["response"]["incomplete_details"]["reason"] == "max_output_tokens"
    assert not any(t == "response.completed" for t, _ in events)


def test_ping_passthrough():
    chunks = frames(": ping", {"choices": [{"delta": {"content": "hi"}}]}, "[DONE]")
    resp = bridge.anthropic_stream_relay(FakeInner(chunks), "m", prefix="t")
    text = "".join(collect(resp))
    assert ": ping" in text, "心跳注释必须透传"


def test_with_pings_keeps_order_and_inserts():
    def slow_source():
        yield "a"
        time.sleep(0.35)
        yield "b"

    got = list(bridge.with_pings(slow_source(), interval=0.1, ping=b"PING"))
    # a ... PING(至少一帧) ... b，顺序不乱
    assert got[0] == "a" and got[-1] == "b"
    assert b"PING" in got[1:-1], got


# Persistent regressions: relay ownership must release synthetic upstream streams.
class _CloseTrackingIterator:
    def __init__(self, chunks=(), *, read_error=None, close_error=None, wait=False):
        self.chunks = iter(chunks)
        self.read_error = read_error
        self.close_error = close_error
        self.wait = wait
        self.close_calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.read_error is not None:
                raise self.read_error
            if self.wait:
                await asyncio.Event().wait()
            raise StopAsyncIteration

    async def aclose(self):
        self.close_calls += 1
        await asyncio.sleep(0)
        if self.close_error is not None:
            raise self.close_error


_RELAY_FACTORIES = (bridge.anthropic_stream_relay, bridge.responses_stream_relay)


def test_relays_release_inner_once_on_eof_inband_error_and_read_failure():
    async def check():
        read_failure = OSError("synthetic read failure")
        for factory in _RELAY_FACTORIES:
            for mode in ("eof", "inband", "read-error"):
                chunks = frames({"choices": [{"delta": {"content": "hi"}}]})
                if mode == "inband":
                    chunks += frames({"error": {"type": "synthetic", "message": "failed"}})
                stream = _CloseTrackingIterator(
                    chunks, read_error=read_failure if mode == "read-error" else None,
                    close_error=RuntimeError("synthetic cleanup failure"))
                response = factory(types.SimpleNamespace(body_iterator=stream), "m")
                if mode == "read-error":
                    with pytest.raises(OSError) as caught:
                        async for _piece in response.body_iterator:
                            pass
                    assert caught.value is read_failure
                else:
                    output = [piece async for piece in response.body_iterator]
                    events = parse_events([piece.decode() for piece in output])
                    if mode == "inband":
                        assert any(kind in ("error", "response.failed") for kind, _ in events)
                    else:
                        assert events[-1][0] in ("message_stop", "response.completed")
                assert stream.close_calls == 1
                await response.body_iterator.aclose()
                assert stream.close_calls == 1
    asyncio.run(check())


def test_relays_release_unread_inner_when_consumer_closes_after_initial_event():
    async def check():
        for factory in _RELAY_FACTORIES:
            stream = _CloseTrackingIterator(wait=True)
            response = factory(types.SimpleNamespace(body_iterator=stream), "m")
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
            await response.body_iterator.aclose()
            assert stream.close_calls == 1
    asyncio.run(check())


def test_relays_release_inner_on_asgi_disconnect_and_send_failure():
    from starlette.requests import ClientDisconnect

    async def check():
        for factory in _RELAY_FACTORIES:
            for mode in ("disconnect", "start-error", "body-error"):
                stream = _CloseTrackingIterator(wait=True)
                response = factory(types.SimpleNamespace(body_iterator=stream), "m")
                sent = asyncio.Event()

                async def receive():
                    await sent.wait()
                    return {"type": "http.disconnect"}

                async def send(message):
                    if (mode == "start-error" and message["type"] == "http.response.start"
                            or mode == "body-error" and message["type"] == "http.response.body"):
                        raise OSError("synthetic disconnected client")
                    if message["type"] == "http.response.body":
                        sent.set()

                scope = {"type": "http", "asgi": {"spec_version": "2.0" if mode == "disconnect" else "2.4"}}
                if mode == "disconnect":
                    await asyncio.wait_for(response(scope, receive, send), 2)
                else:
                    with pytest.raises(ClientDisconnect):
                        await asyncio.wait_for(response(scope, receive, send), 2)
                assert stream.close_calls == 1
    asyncio.run(check())


def test_relays_preserve_cancellation_and_cleanup_cancellation():
    async def check():
        for factory in _RELAY_FACTORIES:
            stream = _CloseTrackingIterator(wait=True)
            response = factory(types.SimpleNamespace(body_iterator=stream), "m")
            sent = asyncio.Event()

            async def receive():
                await asyncio.Event().wait()

            async def send(message):
                if message["type"] == "http.response.body":
                    sent.set()

            task = asyncio.create_task(response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
            await asyncio.wait_for(sent.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.close_calls == 1

            stream = _CloseTrackingIterator(close_error=asyncio.CancelledError())
            response = factory(types.SimpleNamespace(body_iterator=stream), "m")
            with pytest.raises(asyncio.CancelledError):
                async for _piece in response.body_iterator:
                    pass
            assert stream.close_calls == 1
    asyncio.run(check())


def test_relays_release_async_context_resources_on_early_error_and_eof():
    async def check():
        for factory in _RELAY_FACTORIES:
            for mode in ("close", "error", "eof"):
                exited = []

                class Resource:
                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args):
                        await asyncio.sleep(0)
                        exited.append(True)

                async def source():
                    async with Resource():
                        yield frames({"choices": [{"delta": {"content": "hi"}}]})[0]
                        if mode == "error":
                            raise OSError("synthetic read failure")

                response = factory(types.SimpleNamespace(body_iterator=source()), "m")
                if mode == "close":
                    # Initial event precedes source consumption; wait for real text.
                    while b'"hi"' not in await response.body_iterator.__anext__():
                        pass
                    await response.body_iterator.aclose()
                elif mode == "error":
                    with pytest.raises(OSError):
                        async for _piece in response.body_iterator:
                            pass
                else:
                    async for _piece in response.body_iterator:
                        pass
                assert exited == [True]
    asyncio.run(check())


def test_relays_release_async_iterators_with_only_synchronous_close():
    class SyncCloseIterator:
        def __init__(self):
            self.close_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        def close(self):
            self.close_calls += 1

    async def check():
        for factory in _RELAY_FACTORIES:
            stream = SyncCloseIterator()
            response = factory(types.SimpleNamespace(body_iterator=stream), "m")
            async for _piece in response.body_iterator:
                pass
            await response.body_iterator.aclose()
            assert stream.close_calls == 1
    asyncio.run(check())


def test_relays_preserve_read_failure_when_cleanup_is_cancelled():
    async def check():
        for factory in _RELAY_FACTORIES:
            original = OSError("synthetic read failure")
            cancelled = asyncio.CancelledError("synthetic cleanup cancellation")
            stream = _CloseTrackingIterator(read_error=original, close_error=cancelled)
            response = factory(types.SimpleNamespace(body_iterator=stream), "m")
            with pytest.raises(OSError) as caught:
                async for _piece in response.body_iterator:
                    pass
            assert caught.value is original
            assert caught.value.__cause__ is cancelled
            assert stream.close_calls == 1
    asyncio.run(check())


def test_concurrent_relay_close_waits_for_pending_read_and_releases_resource_once():
    async def check():
        for factory in _RELAY_FACTORIES:
            entered = asyncio.Event()
            closed = []

            async def upstream():
                try:
                    entered.set()
                    await asyncio.Event().wait()
                    yield b'data: {"choices":[{"delta":{"content":"unreachable"}}]}\n\n'
                finally:
                    await asyncio.sleep(0)
                    closed.append(True)

            raw = upstream()
            response = factory(types.SimpleNamespace(body_iterator=raw), "m")
            await response.body_iterator.__anext__()
            reader = asyncio.create_task(response.body_iterator.__anext__())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(asyncio.gather(
                    response.body_iterator.aclose(), response.body_iterator.aclose()), 2)
                assert closed == [True]
                with pytest.raises(asyncio.CancelledError):
                    await reader
                await response.body_iterator.aclose()
                assert closed == [True]
                assert raw.ag_frame is None
                assert response.body_iterator.source.ag_frame is None
            finally:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await response.body_iterator.source.aclose()
                await raw.aclose()
    asyncio.run(check())


def test_relays_close_preacquired_async_resource_before_source_starts():
    async def check():
        for factory in _RELAY_FACTORIES:
            released = []

            class AcquiredStream:
                def __aiter__(self):
                    return self

                async def __anext__(self):
                    raise AssertionError("unstarted stream should not be read")

                async def aclose(self):
                    await asyncio.sleep(0)
                    released.append(True)

            response = factory(types.SimpleNamespace(body_iterator=AcquiredStream()), "m")
            await response.body_iterator.aclose()
            await response.body_iterator.aclose()
            assert released == [True]
    asyncio.run(check())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL %s: %s" % (fn.__name__, exc))
    print("== %d/%d passed ==" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
