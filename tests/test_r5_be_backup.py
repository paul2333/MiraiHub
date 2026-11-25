# -*- coding: utf-8 -*-
"""第 5 轮 r5_be：整库备份接 export_guard（第 4 轮 A1）。

运行：python -m pytest tests/test_r5_be_backup.py -q
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

CONFIRM = {"X-Export-Confirm": "include-secrets"}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("EXPORT_SECRETS_REQUIRE_CONFIRM", raising=False)
    from admin.security import require_admin
    import admin.routers.maintenance as mt

    db_file = tmp_path / "src.db"
    engine = create_engine("sqlite:///" + db_file.as_posix())
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE secrets (v TEXT)"))
        conn.execute(text("INSERT INTO secrets VALUES ('refresh-token-xyz')"))
    app = FastAPI()
    app.include_router(mt.router)
    app.dependency_overrides[require_admin] = lambda: True
    audits = []
    with patch.object(mt, "engine", engine), \
            patch("admin.security.audit_secret_access",
                  lambda req, action, scope: audits.append((action, scope)), create=True):
        yield TestClient(app), audits, tmp_path
    engine.dispose()


def test_backup_without_confirm_is_428(env):
    client, audits, _ = env
    r = client.post("/api/maintenance/backup")
    assert r.status_code == 428, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "export_confirm_required"
    assert detail["confirm_header"] == "X-Export-Confirm"
    assert audits == []


def test_backup_with_header_returns_db_and_audits(env):
    client, audits, tmp_path = env
    r = client.post("/api/maintenance/backup", headers=CONFIRM)
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    out = tmp_path / "out.db"
    out.write_bytes(r.content)
    with closing(sqlite3.connect(out)) as conn:
        assert conn.execute("SELECT v FROM secrets").fetchone()[0] == "refresh-token-xyz"
    assert audits == [("credential_export", "db backup")]


def test_backup_with_query_confirm(env):
    client, _, _ = env
    r = client.post("/api/maintenance/backup", params={"confirm": "include-secrets"})
    assert r.status_code == 200


def test_backup_wrong_confirm_value_still_428(env):
    client, _, _ = env
    r = client.post("/api/maintenance/backup", headers={"X-Export-Confirm": "yes"})
    assert r.status_code == 428


def test_backup_fallback_switch(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("EXPORT_SECRETS_REQUIRE_CONFIRM", "0")
    r = client.post("/api/maintenance/backup")
    assert r.status_code == 200


def test_mysql_is_409_before_confirm(env):
    client, audits, _ = env
    import admin.routers.maintenance as mt

    fake = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    with patch.object(mt, "engine", fake):
        assert client.post("/api/maintenance/backup").status_code == 409
        assert client.post("/api/maintenance/backup", headers=CONFIRM).status_code == 409
    assert audits == []


def test_backup_route_static_guard():
    src = (ROOT / "admin/routers/maintenance.py").read_text(encoding="utf-8")
    seg = src[src.index('@router.post("/backup")'):][:4000]
    assert 'guard_secret_export(request, "db backup", True)' in seg
    # 409（非 sqlite）在闸门之前
    assert seg.index("!= \"sqlite\"") < seg.index("guard_secret_export(")
