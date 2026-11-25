"""账号冷却范围：只有认证类失败才冷却账号，风控走平台级退避。

2026-11-25：旧实现把 risk_control 也算进 600s 账号冷却，于是单条坏 prompt
命中 4100404 就把账号锁 10 分钟，池子迅速枯竭；本用例把该边界钉死。
"""
from __future__ import annotations

import inspect

from admin.platform.marvis import gateway


def test_auth_failure_cools_account():
    assert gateway._cool_seconds("auth") == 600


def test_risk_control_does_not_cool_account():
    # 风控是平台级退避（note_risk_control），不是账号问题。
    assert gateway._cool_seconds("risk_control") == 0


def test_other_kinds_do_not_cool_account():
    for kind in ("quota_exceeded", "upstream", "rate_limit", "", None):
        assert gateway._cool_seconds(kind) == 0


def test_call_site_uses_helper_not_hardcoded_tuple():
    # 防回归：调用点必须走 helper，不能再出现 auth/risk_control 的硬编码。
    src = inspect.getsource(gateway)
    assert "cool_seconds=_cool_seconds(" in src
    assert 'cool_seconds=(600 if exc.kind in ("auth", "risk_control") else 0)' not in src
