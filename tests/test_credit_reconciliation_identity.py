"""Persistent regressions: credit corrections must keep each request's identity."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def reconciliation(monkeypatch, tmp_path):
    # Importing configuration must not read a developer's .env or open their DB.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("ADMIN_DATABASE_URL", "sqlite:///" + (tmp_path / "unused.db").as_posix())
    monkeypatch.setenv("ADMIN_JWT_SECRET", "synthetic-credit-reconciliation-only")
    from admin.db import Base
    from admin.models import Account, ApiKey, UsageLog
    from admin.routers import proxy
    from admin.platform.workbuddy.core import backend

    engine = create_engine("sqlite://", poolclass=StaticPool)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    created_at = datetime(2026, 11, 22, 10, 0, 0)
    rows = []
    with factory() as db:
        db.add_all([
            Account(id=1, name="shared", uid="synthetic-shared", platform="workbuddy",
                    auth_json="{}", balance_remain=992),
            Account(id=2, name="separate", uid="synthetic-separate", platform="workbuddy",
                    auth_json="{}", balance_remain=996),
        ])
        for number, account_id, credits in ((1, 1, 1), (2, 1, 7), (3, 2, 4)):
            db.add(ApiKey(id=number, name="synthetic", key_hash="synthetic-" + str(number),
                          credit_limit=1000, credit_used=credits, unlimited=0))
            db.add(UsageLog(id=number, api_key_id=number, account_id=account_id,
                            platform="workbuddy", model="synthetic-model", credits=credits,
                            total_tokens=1000, created_at=created_at, error_kind="success"))
        db.commit()

    class AccountSession:
        def __init__(self, auth_json):
            assert auth_json == "{}"

        def fetch_request_usage(self, *args, **kwargs):
            return {"data": {"data": rows}}

        def close(self):
            pass

    monkeypatch.setattr(proxy, "SessionLocal", factory)
    monkeypatch.setattr(proxy.time, "sleep", lambda *args: None)
    monkeypatch.setattr(proxy.settings, "UPSTREAM_CLIENT_NAME", "SyntheticReview")
    monkeypatch.setattr(backend, "AccountSession", AccountSession)

    def correct(log_id=1, account_id=1, request_ids=None):
        proxy._fetch_real_credits(log_id, account_id, "{}", "synthetic-model", 1,
                                 created_at, request_ids=request_ids)

    def snapshot():
        with factory() as db:
            return {
                "logs": tuple(db.get(UsageLog, number).credits for number in (1, 2, 3)),
                "keys": tuple(db.get(ApiKey, number).credit_used for number in (1, 2, 3)),
                "balances": tuple(db.get(Account, number).balance_remain for number in (1, 2)),
            }

    try:
        yield SimpleNamespace(rows=rows, correct=correct, snapshot=snapshot)
    finally:
        engine.dispose()


def billing_row(request_id, credits, *, model="synthetic-model", client="SyntheticReview",
                request_time="2026-11-22 10:00:00"):
    return {"requestId": request_id, "credit": credits, "model": model,
            "client": client, "requestTime": request_time}


def test_exact_request_id_wins_over_closer_same_model_request(reconciliation):
    reconciliation.rows.extend([
        billing_row("other-request", 75),
        billing_row("wanted-request", 3, request_time="2026-11-22 09:59:58"),
    ])

    reconciliation.correct(request_ids=["failed-attempt", "wanted-request"])

    assert reconciliation.snapshot() == {
        "logs": (3, 7, 4), "keys": (3, 7, 4), "balances": (990, 996),
    }


def test_known_request_id_missing_from_delayed_billing_page_keeps_estimate(reconciliation):
    reconciliation.rows.append(billing_row("unrelated-request", 75))

    reconciliation.correct(request_ids=["not-visible-on-this-page"])

    assert reconciliation.snapshot() == {
        "logs": (1, 7, 4), "keys": (1, 7, 4), "balances": (992, 996),
    }


@pytest.mark.parametrize("request_ids", [None, []])
def test_legacy_request_without_ids_keeps_model_client_time_fallback(reconciliation, request_ids):
    reconciliation.rows.extend([
        billing_row("wrong-model", 75, model="different-model"),
        billing_row("wrong-client", 90, client="DifferentClient"),
        billing_row("older-match", 11, request_time="2026-11-22 09:59:15"),
        billing_row("nearest-match", 3, request_time="2026-11-22 09:59:58"),
    ])

    reconciliation.correct(request_ids=request_ids)

    assert reconciliation.snapshot() == {
        "logs": (3, 7, 4), "keys": (3, 7, 4), "balances": (990, 996),
    }


def test_shared_account_does_not_assign_another_keys_request_to_missing_id(reconciliation):
    reconciliation.rows.append(billing_row("key-two-request", 75))

    reconciliation.correct(request_ids=["key-one-request"])
    reconciliation.correct(log_id=2, account_id=1, request_ids=["key-two-request"])

    assert reconciliation.snapshot() == {
        "logs": (1, 75, 4), "keys": (1, 75, 4), "balances": (924, 996),
    }


def test_repeated_exact_id_correction_does_not_debit_twice(reconciliation):
    reconciliation.rows.append(billing_row("wanted-request", 3))

    reconciliation.correct(request_ids=["wanted-request"])
    reconciliation.correct(request_ids=["wanted-request"])

    assert reconciliation.snapshot() == {
        "logs": (3, 7, 4), "keys": (3, 7, 4), "balances": (990, 996),
    }
