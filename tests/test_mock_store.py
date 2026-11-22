from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tests.conftest import parse_html


def test_healthz_returns_ok(client) -> None:
    # Kabul: given running app with Postgres, expect GET /healthz returns 200 and {"status": "ok"}
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_jsonld_pages_return_valid_product_offer_and_full_uri_availability(client, db_conn) -> None:
    # Kabul: given seeded jsonld items (InStock and OutOfStock), expect GET /mock-store/{slug}
    # returns JSON-LD Product->offers->Offer with exact price, priceCurrency and full schema.org availability URI
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT slug, name, price, currency, in_stock FROM mock_store_items "
            "WHERE render_mode = 'jsonld' ORDER BY slug"
        )
        rows = cur.fetchall()

    assert len(rows) >= 2
    seen_availabilities: set[str] = set()

    for slug, name, price, currency, in_stock in rows:
        resp = client.get(f"/mock-store/{slug}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

        doc = parse_html(resp.text)
        assert len(doc.jsonld_blocks) == 1, f"expected 1 JSON-LD block for {slug}"

        payload = json.loads(doc.jsonld_blocks[0])
        assert payload["@type"] == "Product"
        assert payload["name"] == name
        assert "offers" in payload and isinstance(payload["offers"], dict)

        offer = payload["offers"]
        assert offer["@type"] == "Offer"
        assert Decimal(str(offer["price"])) == price
        assert offer["priceCurrency"] == currency

        expected_uri = (
            "https://schema.org/InStock" if in_stock else "https://schema.org/OutOfStock"
        )
        assert offer["availability"] == expected_uri
        seen_availabilities.add(offer["availability"])

    assert seen_availabilities == {
        "https://schema.org/InStock",
        "https://schema.org/OutOfStock",
    }


def test_css_only_pages_have_no_jsonld_and_expose_price_and_stock(client, db_conn) -> None:
    # Kabul: given seeded css_only items, expect GET /mock-store/{slug} has no JSON-LD script
    # and contains .price and .stock elements with the item's price and stock status
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT slug, price, currency, in_stock FROM mock_store_items "
            "WHERE render_mode = 'css_only' ORDER BY slug"
        )
        rows = cur.fetchall()

    assert len(rows) >= 2

    for slug, price, currency, in_stock in rows:
        resp = client.get(f"/mock-store/{slug}")
        assert resp.status_code == 200
        assert "application/ld+json" not in resp.text

        doc = parse_html(resp.text)
        assert doc.jsonld_blocks == []
        assert len(doc.class_elements.get("price", [])) >= 1
        assert len(doc.class_elements.get("stock", [])) >= 1

        price_text = doc.class_elements["price"][0]
        assert f"{price:.2f}" in price_text
        assert currency in price_text or currency in resp.text

        stock_text = doc.class_elements["stock"][0].lower()
        if in_stock:
            assert "stokta" in stock_text or "in stock" in stock_text
        else:
            assert "tükendi" in stock_text or "out of stock" in stock_text


def test_broken_pages_have_neither_jsonld_nor_price_class(client, db_conn) -> None:
    # Kabul: given seeded broken items, expect GET /mock-store/{slug} returns 200 broken HTML
    # with neither application/ld+json nor .price elements
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT slug FROM mock_store_items WHERE render_mode = 'broken' ORDER BY slug"
        )
        rows = cur.fetchall()

    assert len(rows) >= 2

    for (slug,) in rows:
        resp = client.get(f"/mock-store/{slug}")
        assert resp.status_code == 200
        assert "application/ld+json" not in resp.text

        doc = parse_html(resp.text)
        assert doc.jsonld_blocks == []
        assert doc.class_elements.get("price", []) == []


def test_unknown_slug_returns_404(client) -> None:
    # Kabul: given a slug not present in mock_store_items, expect GET /mock-store/{slug} returns 404
    resp = client.get("/mock-store/bilinmeyen-urun-slug-404")
    assert resp.status_code == 404


@pytest.mark.parametrize("render_mode", ["jsonld", "css_only", "broken"])
def test_mock_store_escapes_html_and_json_safely(client, db_conn, render_mode: str) -> None:
    # Kabul: given an item name containing script/HTML injection characters, expect response
    # escapes HTML tags and prevents </script> breakout while keeping JSON-LD parseable
    dangerous_name = '</script><img src=x onerror="alert(1)"> & "test"'
    slug = f"xss-test-{render_mode}"

    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mock_store_items (slug, name, price, currency, in_stock, render_mode)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (slug, dangerous_name, Decimal("123.45"), "TRY", True, render_mode),
        )
    db_conn.commit()

    resp = client.get(f"/mock-store/{slug}")
    assert resp.status_code == 200
    assert "<img src=x" not in resp.text

    if render_mode == "jsonld":
        doc = parse_html(resp.text)
        assert len(doc.jsonld_blocks) == 1
        payload = json.loads(doc.jsonld_blocks[0])
        assert payload["name"] == dangerous_name
