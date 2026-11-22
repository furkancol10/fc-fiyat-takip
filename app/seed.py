from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg

SEED_PRODUCTS: list[dict[str, Any]] = [
    {
        "sku": "SKU-KAHVE-001",
        "name": "Filtre Kahve Makinesi 1.2L",
        "my_price": Decimal("2599.00"),
        "currency": "TRY",
    },
    {
        "sku": "SKU-TERMOS-002",
        "name": "Çelik Vakumlu Termos 750ml",
        "my_price": Decimal("699.00"),
        "currency": "TRY",
    },
    {
        "sku": "SKU-LAMBA-003",
        "name": "Şarjlı LED Masa Lambası",
        "my_price": Decimal("599.00"),
        "currency": "TRY",
    },
]

SEED_MOCK_STORE_ITEMS: list[dict[str, Any]] = [
    {
        "slug": "kahve-diyari-filtre-makine",
        "name": "Filtre Kahve Makinesi 1.2L",
        "price": Decimal("2499.90"),
        "currency": "TRY",
        "in_stock": True,
        "render_mode": "jsonld",
    },
    {
        "slug": "kamp-doga-celik-termos",
        "name": "Çelik Vakumlu Termos 750ml",
        "price": Decimal("649.50"),
        "currency": "TRY",
        "in_stock": False,
        "render_mode": "jsonld",
    },
    {
        "slug": "mutfak-plus-filtre-makine",
        "name": "Filtre Kahve Makinesi 1.2L",
        "price": Decimal("2549.00"),
        "currency": "TRY",
        "in_stock": True,
        "render_mode": "css_only",
    },
    {
        "slug": "ofis-atolyesi-led-lamba",
        "name": "Şarjlı LED Masa Lambası",
        "price": Decimal("589.90"),
        "currency": "TRY",
        "in_stock": False,
        "render_mode": "css_only",
    },
    {
        "slug": "hizli-pazar-celik-termos",
        "name": "Çelik Vakumlu Termos 750ml",
        "price": Decimal("675.00"),
        "currency": "TRY",
        "in_stock": True,
        "render_mode": "broken",
    },
    {
        "slug": "tekno-vitrin-led-lamba",
        "name": "Şarjlı LED Masa Lambası",
        "price": Decimal("559.00"),
        "currency": "TRY",
        "in_stock": True,
        "render_mode": "broken",
    },
]

SEED_COMPETITORS: list[dict[str, Any]] = [
    {
        "product_sku": "SKU-KAHVE-001",
        "store_name": "Kahve Diyarı",
        "url": "/mock-store/kahve-diyari-filtre-makine",
        "parse_mode": "jsonld",
        "css_price_selector": ".price",
        "css_stock_selector": ".stock",
    },
    {
        "product_sku": "SKU-KAHVE-001",
        "store_name": "Mutfak Plus",
        "url": "/mock-store/mutfak-plus-filtre-makine",
        "parse_mode": "css",
        "css_price_selector": ".price",
        "css_stock_selector": ".stock",
    },
    {
        "product_sku": "SKU-TERMOS-002",
        "store_name": "Kamp & Doğa",
        "url": "/mock-store/kamp-doga-celik-termos",
        "parse_mode": "auto",
        "css_price_selector": ".price",
        "css_stock_selector": ".stock",
    },
    {
        "product_sku": "SKU-TERMOS-002",
        "store_name": "Hızlı Pazar",
        "url": "/mock-store/hizli-pazar-celik-termos",
        "parse_mode": "auto",
        "css_price_selector": ".price",
        "css_stock_selector": ".stock",
    },
    {
        "product_sku": "SKU-LAMBA-003",
        "store_name": "Ofis Atölyesi",
        "url": "/mock-store/ofis-atolyesi-led-lamba",
        "parse_mode": "css",
        "css_price_selector": ".price",
        "css_stock_selector": ".stock",
    },
    {
        "product_sku": "SKU-LAMBA-003",
        "store_name": "Tekno Vitrin",
        "url": "/mock-store/tekno-vitrin-led-lamba",
        "parse_mode": "auto",
        "css_price_selector": ".price",
        "css_stock_selector": ".stock",
    },
]

SEED_ALERT_RULES: list[dict[str, Any]] = [
    {
        "competitor_url": "/mock-store/kahve-diyari-filtre-makine",
        "rule_type": "below_price",
        "threshold_value": Decimal("2400.00"),
        "cooldown_min": 60,
    },
    {
        "competitor_url": "/mock-store/mutfak-plus-filtre-makine",
        "rule_type": "pct_drop",
        "threshold_value": Decimal("5.00"),
        "cooldown_min": 60,
    },
    {
        "competitor_url": "/mock-store/kamp-doga-celik-termos",
        "rule_type": "stock_change",
        "threshold_value": None,
        "cooldown_min": 60,
    },
]


def seed_demo_data(conn: psycopg.Connection) -> None:
    """Insert deterministic demo data inside the caller's transaction."""
    product_ids: dict[str, int] = {}
    competitor_ids: dict[str, int] = {}

    with conn.cursor() as cur:
        for item in SEED_MOCK_STORE_ITEMS:
            cur.execute(
                """
                INSERT INTO mock_store_items (slug, name, price, currency, in_stock, render_mode)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    item["slug"],
                    item["name"],
                    item["price"],
                    item["currency"],
                    item["in_stock"],
                    item["render_mode"],
                ),
            )

        for product in SEED_PRODUCTS:
            cur.execute(
                """
                INSERT INTO products (sku, name, my_price, currency)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (
                    product["sku"],
                    product["name"],
                    product["my_price"],
                    product["currency"],
                ),
            )
            product_ids[product["sku"]] = cur.fetchone()[0]

        for comp in SEED_COMPETITORS:
            cur.execute(
                """
                INSERT INTO competitors (
                    product_id, store_name, url, parse_mode,
                    css_price_selector, css_stock_selector
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    product_ids[comp["product_sku"]],
                    comp["store_name"],
                    comp["url"],
                    comp["parse_mode"],
                    comp["css_price_selector"],
                    comp["css_stock_selector"],
                ),
            )
            competitor_ids[comp["url"]] = cur.fetchone()[0]

        for rule in SEED_ALERT_RULES:
            cur.execute(
                """
                INSERT INTO alert_rules (
                    competitor_id, rule_type, threshold_value, cooldown_min
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    competitor_ids[rule["competitor_url"]],
                    rule["rule_type"],
                    rule["threshold_value"],
                    rule["cooldown_min"],
                ),
            )


if __name__ == "__main__":
    from app.db import apply_schema, get_connection
    from app.reset import reset_demo

    with get_connection() as conn:
        apply_schema(conn)
        conn.commit()
        reset_demo(conn)
