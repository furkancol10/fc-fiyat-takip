from __future__ import annotations

from decimal import Decimal

import psycopg
import pytest


def _snapshot_demo_state(conn: psycopg.Connection) -> dict[str, list[tuple]]:
    tables = {
        "products": "SELECT sku, name, my_price, currency FROM products ORDER BY sku",
        "competitors": (
            "SELECT p.sku, c.store_name, c.url, c.parse_mode, c.css_price_selector, "
            "c.css_stock_selector, c.last_price, c.last_in_stock, c.consecutive_failures, c.is_active "
            "FROM competitors c JOIN products p ON p.id = c.product_id ORDER BY p.sku, c.url"
        ),
        "alert_rules": (
            "SELECT c.url, r.rule_type, r.threshold_value, r.cooldown_min, r.is_active "
            "FROM alert_rules r JOIN competitors c ON c.id = r.competitor_id "
            "ORDER BY c.url, r.rule_type"
        ),
        "mock_store_items": (
            "SELECT slug, name, price, currency, in_stock, render_mode "
            "FROM mock_store_items ORDER BY slug"
        ),
        "price_history": "SELECT competitor_id, price, in_stock FROM price_history ORDER BY id",
        "alert_logs": "SELECT rule_id, event_type, status FROM alert_logs ORDER BY id",
    }
    snapshot: dict[str, list[tuple]] = {}
    with conn.cursor() as cur:
        for name, query in tables.items():
            cur.execute(query)
            snapshot[name] = cur.fetchall()
    # Okuma işlemi açık kalırsa ACCESS SHARE kilidi reset_demo() TRUNCATE'ini bekletir.
    conn.rollback()
    return snapshot


def test_seed_has_at_least_two_items_per_render_mode(db_conn) -> None:
    # Kabul: given freshly seeded database, expect at least 2 mock_store_items per render_mode
    # (jsonld, css_only, broken) plus seeded products and competitors
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT render_mode, COUNT(*) FROM mock_store_items GROUP BY render_mode"
        )
        counts = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM products")
        product_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM competitors")
        competitor_count = cur.fetchone()[0]

    assert counts.get("jsonld", 0) >= 2
    assert counts.get("css_only", 0) >= 2
    assert counts.get("broken", 0) >= 2
    assert product_count >= 2
    assert competitor_count >= 6


def test_reset_demo_is_idempotent_and_restores_modified_price(db_conn) -> None:
    # Kabul: given reset_demo() called twice, expect identical database state;
    # given a mutated mock_store price and extra rows, expect reset_demo() restores original seed values
    from app.reset import reset_demo

    reset_demo()
    first_snapshot = _snapshot_demo_state(db_conn)

    reset_demo()
    second_snapshot = _snapshot_demo_state(db_conn)
    assert first_snapshot == second_snapshot

    target_slug, _, original_price, *_ = first_snapshot["mock_store_items"][0]
    mutated_price = original_price + Decimal("999.00")

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE mock_store_items SET price = %s WHERE slug = %s",
            (mutated_price, target_slug),
        )
        cur.execute(
            "INSERT INTO products (sku, name, my_price, currency) VALUES (%s, %s, %s, %s)",
            ("SKU-TEMP-999", "Geçici Ürün", Decimal("100.00"), "TRY"),
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("SELECT price FROM mock_store_items WHERE slug = %s", (target_slug,))
        assert cur.fetchone()[0] == mutated_price
    db_conn.rollback()  # okuma işlemini kapat, yoksa TRUNCATE kilitte bekler

    reset_demo()
    restored_snapshot = _snapshot_demo_state(db_conn)
    assert restored_snapshot == first_snapshot


def test_competitors_unique_product_url_constraint(db_conn) -> None:
    # Kabul: given an existing competitor (product_id, url), expect inserting the same pair
    # fails with UniqueViolation
    with db_conn.cursor() as cur:
        cur.execute("SELECT product_id, url FROM competitors LIMIT 1")
        product_id, url = cur.fetchone()

        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO competitors (product_id, store_name, url) VALUES (%s, %s, %s)",
                (product_id, "Tekrar Mağaza", url),
            )
    db_conn.rollback()


def test_product_delete_cascades_to_competitors_history_rules_and_logs(db_conn) -> None:
    # Kabul: given a product with competitors, price_history, alert_rules and alert_logs,
    # expect deleting the product cascades and removes all dependent child rows
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY id LIMIT 1")
        product_id = cur.fetchone()[0]

        cur.execute("SELECT id FROM competitors WHERE product_id = %s", (product_id,))
        competitor_ids = [row[0] for row in cur.fetchall()]
        assert len(competitor_ids) >= 1

        comp_id = competitor_ids[0]
        cur.execute(
            "INSERT INTO price_history (competitor_id, price, in_stock) VALUES (%s, %s, %s)",
            (comp_id, Decimal("1999.90"), True),
        )
        cur.execute(
            """
            INSERT INTO alert_rules (competitor_id, rule_type, threshold_value, cooldown_min)
            VALUES (%s, 'below_price', %s, 60)
            RETURNING id
            """,
            (comp_id, Decimal("2000.00")),
        )
        rule_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO alert_logs (rule_id, event_type, old_val, new_val, status)
            VALUES (%s, 'below_price', '2499.90', '1999.90', 'dry_run')
            """,
            (rule_id,),
        )
        db_conn.commit()

        cur.execute("DELETE FROM products WHERE id = %s", (product_id,))
        db_conn.commit()

        cur.execute("SELECT COUNT(*) FROM competitors WHERE product_id = %s", (product_id,))
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT COUNT(*) FROM price_history WHERE competitor_id = ANY(%s)", (competitor_ids,))
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT COUNT(*) FROM alert_rules WHERE competitor_id = ANY(%s)", (competitor_ids,))
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT COUNT(*) FROM alert_logs WHERE rule_id = %s", (rule_id,))
        assert cur.fetchone()[0] == 0


def test_check_constraints_reject_invalid_render_mode_and_rule_type(db_conn) -> None:
    # Kabul: given invalid mock_store_items.render_mode or alert_rules.rule_type,
    # expect PostgreSQL CHECK constraints reject inserts with CheckViolation
    with db_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO mock_store_items (slug, name, price, currency, in_stock, render_mode)
                VALUES ('gecersiz-mod', 'Bozuk Mod', 100.00, 'TRY', TRUE, 'invalid_mode')
                """
            )
    db_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM competitors LIMIT 1")
        competitor_id = cur.fetchone()[0]

        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO alert_rules (competitor_id, rule_type, threshold_value)
                VALUES (%s, 'invalid_rule', 50.00)
                """,
                (competitor_id,),
            )
    db_conn.rollback()
