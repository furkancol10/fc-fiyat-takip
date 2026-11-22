from __future__ import annotations

from decimal import Decimal

import pytest

from app.checker import ADVISORY_LOCK_KEY
from app.db import get_connection
from app.limits import check_cooldown


def test_jobs_check_without_header_returns_401_and_does_not_advance_cooldown(
    wired_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given JOB_TOKEN configured, when POST /jobs/check has no X-Job-Token header,
    # expect 401 and global cooldown counter does not advance so a subsequent valid call returns 200.
    monkeypatch.setenv("JOB_TOKEN", "super-secret-job-token")

    resp_missing = wired_client.post("/jobs/check")
    assert resp_missing.status_code == 401

    resp_valid = wired_client.post(
        "/jobs/check", headers={"X-Job-Token": "super-secret-job-token"}
    )
    assert resp_valid.status_code == 200
    body = resp_valid.json()
    assert body["checked"] == 6
    assert body["skipped"] is False


def test_jobs_check_wrong_token_returns_401_and_does_not_advance_cooldown(
    wired_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given JOB_TOKEN configured, when POST /jobs/check sends an incorrect X-Job-Token,
    # expect 401 and cooldown counter does not advance.
    monkeypatch.setenv("JOB_TOKEN", "correct-token-123")

    resp_wrong = wired_client.post("/jobs/check", headers={"X-Job-Token": "wrong-token"})
    assert resp_wrong.status_code == 401

    resp_ok = wired_client.post("/jobs/check", headers={"X-Job-Token": "correct-token-123"})
    assert resp_ok.status_code == 200
    assert resp_ok.json()["checked"] == 6


def test_jobs_check_when_job_token_unset_returns_503(
    wired_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given JOB_TOKEN is unset or empty, when POST /jobs/check is called,
    # expect 503 (never open without a configured token).
    monkeypatch.delenv("JOB_TOKEN", raising=False)
    resp_unset = wired_client.post("/jobs/check", headers={"X-Job-Token": "anything"})
    assert resp_unset.status_code == 503

    monkeypatch.setenv("JOB_TOKEN", "   ")
    resp_blank = wired_client.post("/jobs/check", headers={"X-Job-Token": "   "})
    assert resp_blank.status_code == 503


def test_jobs_check_returns_summary_json_and_enforces_30s_cooldown_with_retry_after(
    wired_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given valid JOB_TOKEN and injectable clock, expect first POST /jobs/check returns 200
    # with CheckSummary JSON, immediate second call returns 429 + Retry-After, and after >=30s returns 200.
    monkeypatch.setenv("JOB_TOKEN", "tok-cool")
    now_t = [1000.0]
    check_cooldown.set_clock(lambda: now_t[0])

    first = wired_client.post("/jobs/check", headers={"X-Job-Token": "tok-cool"})
    assert first.status_code == 200
    data = first.json()
    for key in (
        "checked",
        "changed",
        "alerts_sent",
        "alerts_dry_run",
        "alerts_failed",
        "errors",
        "skipped",
    ):
        assert key in data
    assert data["checked"] == 6
    assert data["errors"] == 2
    assert data["skipped"] is False

    now_t[0] += 5.0
    second = wired_client.post("/jobs/check", headers={"X-Job-Token": "tok-cool"})
    assert second.status_code == 429
    assert "retry-after" in second.headers
    retry_after = int(second.headers["retry-after"])
    assert 20 <= retry_after <= 30

    now_t[0] += 26.0  # total 31s elapsed since first run
    third = wired_client.post("/jobs/check", headers={"X-Job-Token": "tok-cool"})
    assert third.status_code == 200
    assert third.json()["skipped"] is False


def test_jobs_check_returns_skipped_true_when_advisory_lock_held(
    wired_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given another connection holds the PostgreSQL advisory lock,
    # expect POST /jobs/check returns 200 with skipped=True.
    monkeypatch.setenv("JOB_TOKEN", "tok-lock")
    with get_connection() as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        lock_conn.commit()
        try:
            resp = wired_client.post("/jobs/check", headers={"X-Job-Token": "tok-lock"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["skipped"] is True
            assert body["checked"] == 0
        finally:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            lock_conn.commit()


def test_jobs_reset_demo_requires_token_and_restores_seed_prices(
    wired_client, db_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given modified mock_store_items price and JOB_TOKEN configured, expect /jobs/reset-demo
    # rejects missing/wrong token (401) and unset JOB_TOKEN (503), and with valid token returns 200
    # {"status":"ok"} and restores the seed price.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE mock_store_items SET price = %s, in_stock = FALSE "
            "WHERE slug = 'kahve-diyari-filtre-makine'",
            (Decimal("1111.11"),),
        )
    db_conn.commit()

    monkeypatch.delenv("JOB_TOKEN", raising=False)
    assert wired_client.post("/jobs/reset-demo").status_code == 503

    monkeypatch.setenv("JOB_TOKEN", "tok-reset")
    assert wired_client.post("/jobs/reset-demo").status_code == 401
    assert (
        wired_client.post("/jobs/reset-demo", headers={"X-Job-Token": "bad"}).status_code
        == 401
    )

    ok_resp = wired_client.post(
        "/jobs/reset-demo", headers={"X-Job-Token": "tok-reset"}
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {"status": "ok"}

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT price, in_stock FROM mock_store_items WHERE slug = 'kahve-diyari-filtre-makine'"
        )
        price, in_stock = cur.fetchone()
    db_conn.commit()

    assert price == Decimal("2499.90")
    assert in_stock is True
