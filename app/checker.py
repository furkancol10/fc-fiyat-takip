from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.fetcher import FetchError, fetch_html
from app.parser import ParseError, parse_product
from app.rules import MIN_PRICE_DIFF, AlertEvent, AlertRule, evaluate
from app.security import DEFAULT_MOCK_BASE_URL, UrlNotAllowed

ADVISORY_LOCK_KEY = 741928365


@dataclass(frozen=True)
class CheckSummary:
    checked: int = 0
    changed: int = 0
    alerts_sent: int = 0
    alerts_dry_run: int = 0
    alerts_failed: int = 0
    errors: int = 0
    skipped: bool = False


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _derive_base_url(policy: Any, base_url: str | None) -> str:
    if base_url is not None:
        return base_url.rstrip("/")

    allowed_hosts = getattr(policy, "allowed_hosts", None)
    if allowed_hosts is None and isinstance(policy, tuple) and len(policy) >= 1:
        allowed_hosts = policy[0]

    hosts = list(allowed_hosts) if allowed_hosts else []
    if len(hosts) == 1:
        host = str(hosts[0]).strip().strip("[]")
        port = getattr(policy, "port", None)
        host_fmt = f"[{host}]" if ":" in host else host
        if port == 443:
            return f"https://{host_fmt}"
        if port is None or port == 80:
            return f"http://{host_fmt}"
        return f"http://{host_fmt}:{port}"

    return (os.environ.get("MOCK_BASE_URL") or DEFAULT_MOCK_BASE_URL).rstrip("/")


def _resolve_target_url(raw_url: str, policy: Any, base_url: str | None) -> str:
    if raw_url.startswith("/") and not raw_url.startswith("//"):
        return f"{_derive_base_url(policy, base_url)}{raw_url}"
    return raw_url


def _has_changed(
    old_price: Decimal | None,
    new_price: Decimal,
    old_in_stock: bool | None,
    new_in_stock: bool | None,
) -> bool:
    if old_price is None:
        return False
    price_changed = abs(new_price - old_price) >= MIN_PRICE_DIFF
    stock_changed = (
        old_in_stock is not None
        and new_in_stock is not None
        and old_in_stock != new_in_stock
    )
    return price_changed or stock_changed


def _record_failure(
    conn: psycopg.Connection,
    competitor_id: int,
    status_code: str,
    check_time: datetime,
) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE competitors
                SET last_status = %s,
                    consecutive_failures = consecutive_failures + 1,
                    last_checked_at = %s
                WHERE id = %s
                """,
                (status_code, check_time, competitor_id),
            )


def _unlock_advisory(conn: psycopg.Connection) -> None:
    try:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
        conn.commit()
    except Exception:
        pass


def run_check(
    conn: psycopg.Connection,
    *,
    fetch: Callable[..., str] = fetch_html,
    notifier: Any,
    policy: Any,
    now: datetime | None = None,
    base_url: str | None = None,
) -> CheckSummary:
    """Run one advisory-locked check cycle across all active competitors."""
    check_time = _normalize_now(now)

    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        acquired = bool(cur.fetchone()[0])
    conn.commit()

    if not acquired:
        return CheckSummary(skipped=True)

    checked = 0
    changed = 0
    alerts_sent = 0
    alerts_dry_run = 0
    alerts_failed = 0
    errors = 0

    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                    c.id,
                    c.product_id,
                    p.name AS product_name,
                    c.store_name,
                    c.url,
                    c.parse_mode,
                    c.css_price_selector,
                    c.css_stock_selector,
                    c.last_price,
                    c.last_in_stock
                FROM competitors c
                JOIN products p ON p.id = c.product_id
                WHERE c.is_active = TRUE
                ORDER BY c.id ASC
                """
            )
            competitors = cur.fetchall()
        conn.commit()

        for comp in competitors:
            checked += 1
            comp_id: int = comp["id"]
            target_url = _resolve_target_url(comp["url"], policy, base_url)

            try:
                html = fetch(target_url, policy=policy)
                parsed = parse_product(
                    html,
                    parse_mode=comp["parse_mode"],
                    css_price_selector=comp["css_price_selector"],
                    css_stock_selector=comp["css_stock_selector"],
                )
            except UrlNotAllowed:
                _record_failure(conn, comp_id, "url_not_allowed", check_time)
                errors += 1
                continue
            except FetchError:
                _record_failure(conn, comp_id, "fetch_error", check_time)
                errors += 1
                continue
            except ParseError:
                _record_failure(conn, comp_id, "parse_error", check_time)
                errors += 1
                continue
            except Exception:
                _record_failure(conn, comp_id, "unexpected_error", check_time)
                errors += 1
                continue

            old_price: Decimal | None = comp["last_price"]
            old_in_stock: bool | None = comp["last_in_stock"]
            history_in_stock = (
                parsed.in_stock
                if parsed.in_stock is not None
                else (old_in_stock if old_in_stock is not None else True)
            )

            try:
                with conn.transaction():
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(
                            """
                            INSERT INTO price_history (competitor_id, price, in_stock, checked_at)
                            VALUES (%s, %s, %s, %s)
                            """,
                            (comp_id, parsed.price, history_in_stock, check_time),
                        )
                        cur.execute(
                            """
                            UPDATE competitors
                            SET last_price = %s,
                                last_in_stock = COALESCE(%s, last_in_stock),
                                last_status = 'ok',
                                consecutive_failures = 0,
                                last_checked_at = %s
                            WHERE id = %s
                            """,
                            (parsed.price, parsed.in_stock, check_time, comp_id),
                        )
                        cur.execute(
                            """
                            SELECT
                                id,
                                rule_type,
                                threshold_value,
                                cooldown_min,
                                last_sent_at,
                                is_active
                            FROM alert_rules
                            WHERE competitor_id = %s AND is_active = TRUE
                            ORDER BY id ASC
                            """,
                            (comp_id,),
                        )
                        rule_rows = cur.fetchall()
            except Exception:
                _record_failure(conn, comp_id, "unexpected_error", check_time)
                errors += 1
                continue

            if not _has_changed(old_price, parsed.price, old_in_stock, parsed.in_stock):
                continue

            changed += 1
            pending_logs: list[tuple[int, AlertEvent, str]] = []

            for rule_row in rule_rows:
                rule = AlertRule(
                    id=rule_row["id"],
                    rule_type=rule_row["rule_type"],
                    threshold_value=rule_row["threshold_value"],
                    cooldown_min=rule_row["cooldown_min"],
                    last_sent_at=rule_row["last_sent_at"],
                    is_active=rule_row["is_active"],
                )
                event = evaluate(
                    rule,
                    old_price=old_price,
                    new_price=parsed.price,
                    old_in_stock=old_in_stock,
                    new_in_stock=parsed.in_stock,
                    now=check_time,
                )
                if event is None:
                    continue

                full_message = (
                    f"[{comp['product_name']} / {comp['store_name']}] {event.message}"
                )
                try:
                    status = notifier.send(full_message)
                except Exception:
                    status = "failed"
                if status not in ("sent", "failed", "dry_run"):
                    status = "failed"
                pending_logs.append((rule_row["id"], event, status))

            if pending_logs:
                with conn.transaction():
                    with conn.cursor() as cur:
                        for rule_id, event, status in pending_logs:
                            cur.execute(
                                """
                                INSERT INTO alert_logs (
                                    rule_id, event_type, old_val, new_val, status, created_at
                                )
                                VALUES (%s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    rule_id,
                                    event.event_type,
                                    event.old_val,
                                    event.new_val,
                                    status,
                                    check_time,
                                ),
                            )
                            if status in ("sent", "dry_run"):
                                cur.execute(
                                    """
                                    UPDATE alert_rules
                                    SET last_sent_at = %s
                                    WHERE id = %s
                                    """,
                                    (check_time, rule_id),
                                )

                for _, _, status in pending_logs:
                    if status == "sent":
                        alerts_sent += 1
                    elif status == "dry_run":
                        alerts_dry_run += 1
                    else:
                        alerts_failed += 1

        return CheckSummary(
            checked=checked,
            changed=changed,
            alerts_sent=alerts_sent,
            alerts_dry_run=alerts_dry_run,
            alerts_failed=alerts_failed,
            errors=errors,
            skipped=False,
        )
    finally:
        _unlock_advisory(conn)
