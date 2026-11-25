"""max_tokens 参数上限：>131072 截断，0/负数关闭（DSH 3000101 根因）。"""
from __future__ import annotations

from admin.platform.marvis import gateway


def test_default_cap_is_131072(monkeypatch):
    monkeypatch.delenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, raising=False)
    assert gateway._max_tokens_param_cap() == 131072


def test_env_override(monkeypatch):
    monkeypatch.setenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, "65536")
    assert gateway._max_tokens_param_cap() == 65536


def test_env_zero_disables(monkeypatch):
    monkeypatch.setenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, "0")
    assert gateway._max_tokens_param_cap() == 0


def test_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, "abc")
    assert gateway._max_tokens_param_cap() == 131072


def test_clamp_228000(monkeypatch):
    monkeypatch.delenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, raising=False)
    body = {"max_tokens": 228000}
    assert gateway._clamp_max_tokens(body) == 228000
    assert body["max_tokens"] == 131072


def test_clamp_leaves_small_value(monkeypatch):
    monkeypatch.delenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, raising=False)
    body = {"max_tokens": 4096}
    assert gateway._clamp_max_tokens(body) == 0
    assert body["max_tokens"] == 4096


def test_clamp_ignores_non_number(monkeypatch):
    monkeypatch.delenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, raising=False)
    body = {"max_tokens": "lots"}
    assert gateway._clamp_max_tokens(body) == 0
    assert body["max_tokens"] == "lots"


def test_clamp_disabled_by_env(monkeypatch):
    monkeypatch.setenv(gateway.MAX_TOKENS_PARAM_CAP_ENV, "0")
    body = {"max_tokens": 228000}
    assert gateway._clamp_max_tokens(body) == 0
    assert body["max_tokens"] == 228000
