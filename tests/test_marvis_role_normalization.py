"""Marvis 角色归一化（2026-11-19）—— developer -> system / 合并 / 顶层 system 并入 /
max_completion_tokens 映射 / GRACE 默认值。

## 为什么必须锁住

上游只认 role=system。同一把 key、同一个网关、同一个 ``main-auto`` 模型，只把
首条消息的 role 从 system 换成 developer（2026-11-19 真机对照）：

  * 短提示词（61 tools + stream + reasoning_effort:high）-> 400 / 3000101（~4.0s）
  * 9 KB 提示词 + 61 tools + stream                    -> 403 / 4100406（43~57s）

触发方在客户端：DSH 的 pi-ai 适配器把「非标准 baseURL」一律当
``supportsDeveloperRole=true``，而 marvis 条目声明了 reasoningEfforts
（reasoning=true），两者相乘 ⇒ 线上**每条**请求的首条消息都是 developer。
客户端版本不受网关控制，所以归一化是唯一兜得住的地方。

## 断言口径

``_prepare_body`` 的输出就是 ``_acquire`` -> ``_call_upstream`` 的入参；另有一条
用假 ``_acquire`` 把 ``chat_completions`` 真正送进去的 body 抓下来，锁住
「送上游的首条 role == system」这件事不只在单函数层面成立。

content **逐字**比对：合并只允许在两段之间插入 ``"\n\n"``，每一段的首尾空白、
段落、换行都不许动。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin.platform.marvis import gateway as marvis_gateway  # noqa: E402


TEXT = "You are a capable AI coding assistant.\n\n保持段落，首尾空白也要原样。\n"
ARRAY = [{"type": "text", "text": "line-1\n"},
         {"type": "text", "text": "line-2"}]


def _body(role: str, content) -> dict:
    return {"model": "main-auto",
            "messages": [{"role": role, "content": content},
                         {"role": "user", "content": "hi"}]}


# ---------------------------------------------------------------------------
# ① 四种组合：developer/system x 字符串/数组
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["developer", "system"])
@pytest.mark.parametrize("content", [TEXT, ARRAY], ids=["string", "array"])
def test_prepare_body_first_message_is_system(role, content):
    """送上游的首条消息 role 必须是 system，content 逐字（含对象）未变。"""
    out = marvis_gateway._prepare_body(_body(role, content), "main-auto")
    first = out["messages"][0]
    assert first["role"] == "system"
    assert first["content"] == content
    # 单条 system 不需要合并：content 必须是**同一个对象**，不许重建。
    assert first["content"] is content
    assert [m["role"] for m in out["messages"]] == ["system", "user"]


def test_single_system_message_leaves_messages_untouched():
    """只有一条 system、没有顶层 system：messages 列表与 content 都不重建。"""
    body = _body("system", TEXT)
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"] is body["messages"]
    assert out["messages"][0] is body["messages"][0]
    assert out["messages"][0]["content"] is body["messages"][0]["content"]


def test_developer_message_keeps_extra_keys():
    """role 改名时其余字段（name 等）原样保留。"""
    body = {"model": "main-auto",
            "messages": [{"role": "developer", "content": TEXT, "name": "sys"},
                         {"role": "user", "content": "hi"}]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"][0] == {"role": "system", "content": TEXT, "name": "sys"}


# ---------------------------------------------------------------------------
# ② 多条 system（含 developer）合并：顺序 + 双换行 + 各自空白原样
# ---------------------------------------------------------------------------
def test_multiple_system_messages_merge_in_order_with_double_newline():
    body = {"model": "main-auto", "messages": [
        {"role": "system", "content": "  S1\n"},
        {"role": "user", "content": "u"},
        {"role": "developer", "content": "D1"},
        {"role": "assistant", "content": "a"},
        {"role": "system", "content": "\nS2  "},
    ]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant"]
    assert out["messages"][0]["content"] == "  S1\n\n\nD1\n\n\nS2  "
    # 未参与合并的消息顺序与内容不变
    assert out["messages"][1] == {"role": "user", "content": "u"}
    assert out["messages"][2] == {"role": "assistant", "content": "a"}


def test_array_system_parts_merge_verbatim_with_separator_block():
    first = [{"type": "text", "text": "a\n"}]
    second = [{"type": "text", "text": "b"}]
    body = {"model": "main-auto", "messages": [
        {"role": "developer", "content": first},
        {"role": "system", "content": second},
        {"role": "user", "content": "u"},
    ]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"][0]["content"] == [
        {"type": "text", "text": "a\n"},
        {"type": "text", "text": "\n\n"},
        {"type": "text", "text": "b"},
    ]
    # 每个 text 块的正文都没被改过
    assert out["messages"][0]["content"][0]["text"] == "a\n"
    assert out["messages"][0]["content"][2]["text"] == "b"


def test_mixed_string_and_array_merge_keeps_each_text():
    body = {"model": "main-auto", "messages": [
        {"role": "system", "content": "plain\n"},
        {"role": "developer", "content": [{"type": "text", "text": "part"}]},
        {"role": "user", "content": "u"},
    ]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    merged = out["messages"][0]["content"]
    assert merged == [{"type": "text", "text": "plain\n"},
                      {"type": "text", "text": "\n\n"},
                      {"type": "text", "text": "part"}]


# ---------------------------------------------------------------------------
# ③ 顶层 system 字段并入
# ---------------------------------------------------------------------------
def test_top_level_system_is_merged_before_message_systems():
    body = {"model": "main-auto", "system": "TOP",
            "messages": [{"role": "system", "content": "S1"},
                         {"role": "user", "content": "u"}]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"][0] == {"role": "system", "content": "TOP\n\nS1"}
    assert "system" not in out
    assert [m["role"] for m in out["messages"]] == ["system", "user"]


def test_top_level_system_only_creates_first_message():
    body = {"model": "main-auto", "system": "TOP",
            "messages": [{"role": "user", "content": "u"}]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert [m["role"] for m in out["messages"]] == ["system", "user"]
    assert out["messages"][0]["content"] == "TOP"
    assert "system" not in out


def test_top_level_system_joins_developer_message():
    body = {"model": "main-auto", "system": "TOP",
            "messages": [{"role": "developer", "content": "D1"},
                         {"role": "user", "content": "u"}]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"][0] == {"role": "system", "content": "TOP\n\nD1"}


# ---------------------------------------------------------------------------
# ④ 顺序：归一化必须发生在指纹改写之前
# ---------------------------------------------------------------------------
def test_developer_content_is_fingerprint_rewritten_after_normalization():
    """developer 正文里的风控触发串必须被改写 —— 只有先归一成 system 才洗得到。"""
    body = {"model": "main-auto",
            "messages": [{"role": "developer", "content": "You are a helpful AI assistant. 继续。"},
                         {"role": "user", "content": "hi"}]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"] == "You are a capable AI assistant. 继续。"


def test_top_level_system_is_fingerprint_rewritten_after_merge():
    body = {"model": "main-auto", "system": "You are a helpful AI assistant.",
            "messages": [{"role": "user", "content": "hi"}]}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["messages"][0]["content"] == "You are a capable AI assistant."


# ---------------------------------------------------------------------------
# ⑤ max_completion_tokens -> max_tokens
# ---------------------------------------------------------------------------
MSG = [{"role": "user", "content": "hi"}]


def test_max_completion_tokens_maps_when_max_tokens_absent():
    out = marvis_gateway._prepare_body(
        {"model": "main-auto", "messages": MSG, "max_completion_tokens": 32000},
        "main-auto")
    assert out["max_tokens"] == 32000
    assert "max_completion_tokens" not in out


def test_max_completion_tokens_wins_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        out = marvis_gateway._prepare_body(
            {"model": "main-auto", "messages": MSG,
             "max_tokens": 1000, "max_completion_tokens": 32000}, "main-auto")
    assert out["max_tokens"] == 32000
    assert "max_completion_tokens" not in out
    assert "max_completion_tokens=32000" in caplog.text


def test_equal_max_tokens_values_collapse_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        out = marvis_gateway._prepare_body(
            {"model": "main-auto", "messages": MSG,
             "max_tokens": 4096, "max_completion_tokens": 4096}, "main-auto")
    assert out["max_tokens"] == 4096
    assert "max_completion_tokens" not in out
    assert "冲突" not in caplog.text


def test_absent_max_completion_tokens_leaves_body_alone():
    body = {"model": "main-auto", "messages": MSG, "max_tokens": 4096}
    out = marvis_gateway._prepare_body(body, "main-auto")
    assert out["max_tokens"] == 4096
    assert "max_completion_tokens" not in out


# ---------------------------------------------------------------------------
# ⑥ 真正送进 _acquire 的 body（端到端一口）
# ---------------------------------------------------------------------------
def _request(body: dict):
    from starlette.requests import Request

    payload = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request({"type": "http", "method": "POST",
                    "path": "/v1/chat/completions",
                    "headers": [(b"content-type", b"application/json")],
                    "query_string": b""}, receive)


async def _noop_bill(*_args, **_kwargs):
    return None


@pytest.mark.parametrize("content", [TEXT, ARRAY], ids=["string", "array"])
def test_chat_completions_sends_system_role_upstream(monkeypatch, content):
    """chat_completions -> _acquire 的 body：首条 role=system，content 逐字未变。"""
    captured = {}

    def fake_acquire(request, body, realm, *, stream):
        captured.update(body)
        return (None, {"choices": [], "usage": {}}, None, time.monotonic())

    monkeypatch.setattr(marvis_gateway, "_acquire", fake_acquire)
    monkeypatch.setattr(marvis_gateway, "_bill_async", _noop_bill)
    monkeypatch.setattr(marvis_gateway, "_usage_of", lambda resp: {})

    async def _run():
        return await marvis_gateway.chat_completions(
            _request(_body("developer", content)))

    response = asyncio.run(_run())
    assert int(getattr(response, "status_code", 0)) == 200
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == content


# ---------------------------------------------------------------------------
# ⑦ GRACE 默认值（2026-11-19 复核：0.8s -> 6.0s）
# ---------------------------------------------------------------------------
def test_stream_status_grace_default_is_six_seconds(monkeypatch):
    monkeypatch.delenv(marvis_gateway.STREAM_STATUS_GRACE_ENV, raising=False)
    assert marvis_gateway._stream_status_grace() == marvis_gateway.STREAM_STATUS_GRACE_DEFAULT
    assert marvis_gateway.STREAM_STATUS_GRACE_DEFAULT == 6.0


def test_stream_status_grace_env_override(monkeypatch):
    monkeypatch.setenv(marvis_gateway.STREAM_STATUS_GRACE_ENV, "0.8")
    assert marvis_gateway._stream_status_grace() == 0.8
