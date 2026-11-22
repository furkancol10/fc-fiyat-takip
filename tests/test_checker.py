from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from psycopg.rows import dict_row

from app.checker import ADVISORY_LOCK_KEY, CheckSummary, run_check
from app.db import get_connection
from app.fetcher import fetch_html
from app.security import demo_policy
from app.telegram import Notifier

T0 = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)


def _make_bridge_fetch(test_client):
    def _fetch(url: str, *, policy):
        return fetch_html(url, client=test_client, policy=policy, sleep=lambda _: None)

    return _fetch


def _counting_dry_run_notifier() -> tuple[Notifier, list[str], httpx.Client]:
    transport_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        transport_calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = Notifier(
        token="123:TEST",
        chat_id="-1001",
        dry_run=True,
        client=http_client,
    )
    return notifier, transport_calls, http_client


def test_first_run_creates_baseline_history_and_no_alerts(client, db_conn) -> None:
    # Kabul: given freshly seeded DB (plus a below_price rule whose threshold exceeds initial price),
    # expect first run_check populates price_history for healthy competitors, records errors for broken ones,
    # emits zero alert_logs (baseline), and makes zero network calls in dry_run mode
    with db_conn.cursor() as cur:
        # Also add a below_price rule with threshold ABOVE initial price (2499.90 < 3000.00)
        # to prove first measurement suppresses alerts even when new_price < threshold.
        cur.execute(
            """
            INSERT INTO alert_rules (competitor_id, rule_type, threshold_value, cooldown_min)
            SELECT id, 'below_price', 3000.00, 60
            FROM competitors
            WHERE url = '/mock-store/kahve-diyari-filtre-makine'
            """
        )
    db_conn.commit()

    notifier, transport_calls, http_client = _counting_dry_run_notifier()
    try:
        policy = demo_policy(str(client.base_url))
        summary = run_check(
            db_conn,
            fetch=_make_bridge_fetch(client),
            notifier=notifier,
            policy=policy,
            now=T0,
        )
    finally:
        http_client.close()

    assert isinstance(summary, CheckSummary)
    assert summary.skipped is False
    assert summary.checked == 6
    assert summary.changed == 0
    assert summary.errors == 2
    assert summary.alerts_sent == 0
    assert summary.alerts_dry_run == 0
    assert summary.alerts_failed == 0
    assert transport_calls == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM price_history")
        history_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM alert_logs")
        logs_count = cur.fetchone()[0]
        cur.execute(
            "SELECT url, last_price, last_in_stock, last_status, consecutive_failures "
            "FROM competitors ORDER BY id"
        )
        comps = cur.fetchall()
    db_conn.commit()

    assert history_count == 4
    assert logs_count == 0
    by_url = {row[0]: row[1:] for row in comps}
    assert by_url["/mock-store/kahve-diyari-filtre-makine"] == (Decimal("2499.90"), True, "ok", 0)
    assert by_url["/mock-store/mutfak-plus-filtre-makine"] == (Decimal("2549.00"), True, "ok", 0)
    assert by_url["/mock-store/kamp-doga-celik-termos"] == (Decimal("649.50"), False, "ok", 0)
    assert by_url["/mock-store/ofis-atolyesi-led-lamba"] == (Decimal("589.90"), False, "ok", 0)
    assert by_url["/mock-store/hizli-pazar-celik-termos"][0] is None
    assert by_url["/mock-store/hizli-pazar-celik-termos"][3] == 1
    assert by_url["/mock-store/tekno-vitrin-led-lamba"][0] is None
    assert by_url["/mock-store/tekno-vitrin-led-lamba"][3] == 1


def test_price_drop_triggers_dry_run_alert_then_same_price_and_cooldown_suppress(
    client, db_conn
) -> None:
    # Kabul: given baseline check complete, when price drops below threshold (below_price) and >=5% (pct_drop),
    # expect 1 dry_run alert_log per triggered rule and last_sent_at populated;
    # expect immediate repeat run (same price) and another drop within cooldown_min emit zero new alert_logs;
    # expect another drop after cooldown_min expires emits a new alert_log
    notifier, transport_calls, http_client = _counting_dry_run_notifier()
    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)

    try:
        # Turn 1: baseline
        run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=T0)

        # Drop kahve-diyari below 2400 (below_price rule) AND mutfak-plus by ~10% (pct_drop rule)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET price = 2350.00 WHERE slug = 'kahve-diyari-filtre-makine'"
            )
            cur.execute(
                "UPDATE mock_store_items SET price = 2290.00 WHERE slug = 'mutfak-plus-filtre-makine'"
            )
        db_conn.commit()

        # Turn 2: both rules fire once with status='dry_run'
        t1 = T0 + timedelta(minutes=5)
        summary2 = run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t1)
        assert summary2.changed == 2
        assert summary2.alerts_dry_run == 2
        assert summary2.alerts_sent == 0
        assert summary2.alerts_failed == 0

        with db_conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT event_type, old_val, new_val, status FROM alert_logs ORDER BY id")
            logs = cur.fetchall()
            cur.execute(
                "SELECT rule_type, last_sent_at FROM alert_rules WHERE rule_type IN ('below_price', 'pct_drop') ORDER BY id"
            )
            rules_after_t1 = cur.fetchall()
        db_conn.commit()

        assert len(logs) == 2
        assert [r["event_type"] for r in logs] == ["below_price", "pct_drop"]
        assert all(r["status"] == "dry_run" for r in logs)
        assert all(r["last_sent_at"] == t1 for r in rules_after_t1)

        # Turn 3: immediate rerun with same prices -> zero new logs
        t2 = t1 + timedelta(minutes=1)
        summary3 = run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t2)
        assert summary3.changed == 0
        assert summary3.alerts_dry_run == 0

        # Turn 4: drop mutfak-plus another ~10% WITHIN 60-min cooldown -> suppressed
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET price = 2000.00 WHERE slug = 'mutfak-plus-filtre-makine'"
            )
        db_conn.commit()

        t3 = t1 + timedelta(minutes=30)
        summary4 = run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t3)
        assert summary4.changed == 1
        assert summary4.alerts_dry_run == 0

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM alert_logs")
            count_within_cooldown = cur.fetchone()[0]
        db_conn.commit()
        assert count_within_cooldown == 2

        # Turn 5: drop mutfak-plus another 10% AFTER cooldown expires -> fires again
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET price = 1800.00 WHERE slug = 'mutfak-plus-filtre-makine'"
            )
        db_conn.commit()

        t4 = t1 + timedelta(minutes=61)
        summary5 = run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t4)
        assert summary5.changed == 1
        assert summary5.alerts_dry_run == 1

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM alert_logs")
            count_after_cooldown = cur.fetchone()[0]
        db_conn.commit()
        assert count_after_cooldown == 3
    finally:
        http_client.close()

    assert transport_calls == []


def test_stock_change_triggers_alert(client, db_conn) -> None:
    # Kabul: given kamp-doga-celik-termos starts out_of_stock on turn 1 and flips to in_stock on turn 2,
    # expect stock_change rule logs 1 dry_run alert and updates last_sent_at
    notifier, transport_calls, http_client = _counting_dry_run_notifier()
    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)

    try:
        run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=T0)

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET in_stock = TRUE WHERE slug = 'kamp-doga-celik-termos'"
            )
        db_conn.commit()

        t1 = T0 + timedelta(minutes=5)
        summary = run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t1)
    finally:
        http_client.close()

    assert summary.changed == 1
    assert summary.alerts_dry_run == 1
    assert transport_calls == []

    with db_conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT event_type, status FROM alert_logs")
        logs = cur.fetchall()
        cur.execute("SELECT last_sent_at FROM alert_rules WHERE rule_type = 'stock_change'")
        last_sent_at = cur.fetchone()["last_sent_at"]
    db_conn.commit()

    assert logs == [{"event_type": "stock_change", "status": "dry_run"}]
    assert last_sent_at == t1


def test_broken_page_preserves_last_price_and_does_not_abort_other_competitors(
    client, db_conn
) -> None:
    # Kabul: given a healthy competitor succeeds on turn 1 then turns 'broken' on turn 2,
    # expect consecutive_failures increments, last_price is preserved, no new price_history row is added for it,
    # and all other healthy competitors are still processed normally
    notifier, _, http_client = _counting_dry_run_notifier()
    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)

    try:
        run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=T0)

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET render_mode = 'broken' WHERE slug = 'ofis-atolyesi-led-lamba'"
            )
        db_conn.commit()

        t1 = T0 + timedelta(minutes=10)
        summary = run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t1)
    finally:
        http_client.close()

    assert summary.checked == 6
    assert summary.errors == 3

    with db_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, last_price, last_status, consecutive_failures
            FROM competitors
            WHERE url = '/mock-store/ofis-atolyesi-led-lamba'
            """
        )
        broken_comp = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM price_history WHERE competitor_id = %s",
            (broken_comp["id"],),
        )
        broken_history_count = cur.fetchone()["cnt"]
        cur.execute(
            """
            SELECT last_checked_at, consecutive_failures
            FROM competitors
            WHERE url = '/mock-store/kahve-diyari-filtre-makine'
            """
        )
        healthy_comp = cur.fetchone()
    db_conn.commit()

    assert broken_comp["last_price"] == Decimal("589.90")
    assert broken_comp["consecutive_failures"] == 1
    assert broken_comp["last_status"] != "ok"
    assert broken_history_count == 1
    assert healthy_comp["last_checked_at"] == t1
    assert healthy_comp["consecutive_failures"] == 0


def test_advisory_lock_held_by_other_connection_skips_run_without_db_writes(
    client, db_conn
) -> None:
    # Kabul: given another DB connection holds pg_advisory_lock(ADVISORY_LOCK_KEY),
    # expect run_check returns CheckSummary(skipped=True) and writes nothing to DB or transport
    notifier, transport_calls, http_client = _counting_dry_run_notifier()
    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)

    with get_connection() as locker_conn:
        with locker_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            assert cur.fetchone()[0] is True
        locker_conn.commit()
        try:
            summary = run_check(
                db_conn,
                fetch=bridge_fetch,
                notifier=notifier,
                policy=policy,
                now=T0,
            )
        finally:
            with locker_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
            locker_conn.commit()
            http_client.close()

    assert summary == CheckSummary(skipped=True)
    assert transport_calls == []

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM price_history")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM alert_logs")
        assert cur.fetchone()[0] == 0
    db_conn.commit()


def test_failed_telegram_send_does_not_set_last_sent_at_and_retries_next_turn(
    client, db_conn
) -> None:
    # Kabul: given Notifier in real send mode where Telegram returns 500 on turn 2 and 200 on turn 3,
    # expect turn 2 logs alert_logs.status='failed' and leaves alert_rules.last_sent_at NULL,
    # and turn 3 (within cooldown_min) retries and logs status='sent' with last_sent_at updated
    response_codes = [500, 200]
    calls = 0
    sent_texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        import json

        sent_texts.append(json.loads(request.content.decode("utf-8"))["text"])
        code = response_codes[min(calls, len(response_codes) - 1)]
        calls += 1
        return httpx.Response(code, json={"ok": code == 200})

    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        notifier = Notifier(
            token="999:LIVE_TEST",
            chat_id="-100999",
            dry_run=False,
            client=http_client,
            sleep=lambda _: None,
        )

        # Turn 1: baseline
        run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=T0)

        # Drop mutfak-plus by ~10% -> pct_drop fires, Telegram 500 -> failed
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET price = 2290.00 WHERE slug = 'mutfak-plus-filtre-makine'"
            )
        db_conn.commit()

        t1 = T0 + timedelta(minutes=5)
        summary_fail = run_check(
            db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t1
        )
        assert summary_fail.alerts_failed == 1
        assert summary_fail.alerts_sent == 0

        with db_conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status FROM alert_logs ORDER BY id")
            logs_after_fail = cur.fetchall()
            cur.execute("SELECT last_sent_at FROM alert_rules WHERE rule_type = 'pct_drop'")
            last_sent_after_fail = cur.fetchone()["last_sent_at"]
        db_conn.commit()

        assert logs_after_fail == [{"status": "failed"}]
        assert last_sent_after_fail is None

        # Turn 3: another >=5% drop only 5 minutes later (within 60-min cooldown) -> succeeds because last_sent_at was NULL
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE mock_store_items SET price = 2000.00 WHERE slug = 'mutfak-plus-filtre-makine'"
            )
        db_conn.commit()

        t2 = t1 + timedelta(minutes=5)
        summary_ok = run_check(
            db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=t2
        )
        assert summary_ok.alerts_sent == 1
        assert summary_ok.alerts_failed == 0

        with db_conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT status FROM alert_logs ORDER BY id")
            logs_after_ok = cur.fetchall()
            cur.execute("SELECT last_sent_at FROM alert_rules WHERE rule_type = 'pct_drop'")
            last_sent_after_ok = cur.fetchone()["last_sent_at"]
        db_conn.commit()

        assert logs_after_ok == [{"status": "failed"}, {"status": "sent"}]
        assert last_sent_after_ok == t2
        assert calls == 2
        assert len(sent_texts) == 2
        assert "Filtre Kahve Makinesi 1.2L" in sent_texts[1]
        assert "Mutfak Plus" in sent_texts[1]


def test_disallowed_url_and_inactive_competitor_handled_cleanly(client, db_conn) -> None:
    # Kabul: given one competitor with an SSRF-disallowed URL and one inactive competitor,
    # expect disallowed URL increments consecutive_failures with last_status='url_not_allowed'
    # without aborting other competitors, and inactive competitor is not checked
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products WHERE sku = 'SKU-KAHVE-001'")
        product_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO competitors (product_id, store_name, url, parse_mode, is_active)
            VALUES (%s, 'SSRF Test', 'http://169.254.169.254/latest/meta-data/', 'auto', TRUE)
            """,
            (product_id,),
        )
        cur.execute(
            """
            INSERT INTO competitors (product_id, store_name, url, parse_mode, is_active)
            VALUES (%s, 'Pasif Mağaza', '/mock-store/kahve-diyari-filtre-makine-pasif', 'auto', FALSE)
            """,
            (product_id,),
        )
    db_conn.commit()

    notifier, _, http_client = _counting_dry_run_notifier()
    try:
        policy = demo_policy(str(client.base_url))
        summary = run_check(
            db_conn,
            fetch=_make_bridge_fetch(client),
            notifier=notifier,
            policy=policy,
            now=T0,
        )
    finally:
        http_client.close()

    assert summary.checked == 7  # 6 seeded active + 1 SSRF active (inactive skipped)
    assert summary.errors == 3   # 2 broken + 1 url_not_allowed

    with db_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT last_status, consecutive_failures, last_price FROM competitors WHERE store_name = 'SSRF Test'"
        )
        ssrf_row = cur.fetchone()
        cur.execute(
            "SELECT last_checked_at, consecutive_failures FROM competitors WHERE store_name = 'Pasif Mağaza'"
        )
        inactive_row = cur.fetchone()
    db_conn.commit()

    assert ssrf_row == {
        "last_status": "url_not_allowed",
        "consecutive_failures": 1,
        "last_price": None,
    }
    assert inactive_row == {"last_checked_at": None, "consecutive_failures": 0}


def test_unknown_stock_reading_keeps_last_known_in_stock(client, db_conn, monkeypatch) -> None:
    # Kabul: given a known last_in_stock and a later parse with in_stock=None,
    # expect last_in_stock is preserved (not overwritten with NULL)
    from types import SimpleNamespace

    import app.checker as checker_mod

    notifier, _calls, http_client = _counting_dry_run_notifier()
    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)
    try:
        run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=T0)
        monkeypatch.setattr(
            checker_mod,
            "parse_product",
            lambda html, **kw: SimpleNamespace(price=Decimal("111.00"), in_stock=None),
        )
        run_check(
            db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy,
            now=T0 + timedelta(minutes=5),
        )
    finally:
        http_client.close()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT last_price, last_in_stock FROM competitors "
            "WHERE url = '/mock-store/kahve-diyari-filtre-makine'"
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row == (Decimal("111.00"), True)


def test_unknown_stock_writes_null_to_price_history(client, db_conn, monkeypatch) -> None:
    # Kabul: given parse with in_stock=None (also on the very first measurement),
    # expect every new price_history row has in_stock IS NULL (no invented True)
    from types import SimpleNamespace

    import app.checker as checker_mod

    notifier, _calls, http_client = _counting_dry_run_notifier()
    policy = demo_policy(str(client.base_url))
    bridge_fetch = _make_bridge_fetch(client)
    monkeypatch.setattr(
        checker_mod,
        "parse_product",
        lambda html, **kw: SimpleNamespace(price=Decimal("111.00"), in_stock=None),
    )
    try:
        run_check(db_conn, fetch=bridge_fetch, notifier=notifier, policy=policy, now=T0)
    finally:
        http_client.close()

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), count(*) FILTER (WHERE in_stock IS NULL) FROM price_history")
        total, nulls = cur.fetchone()
        cur.execute("SELECT count(*) FROM competitors WHERE last_in_stock IS NOT NULL")
        known = cur.fetchone()[0]
    db_conn.commit()
    assert total > 0
    assert nulls == total
    assert known == 0
