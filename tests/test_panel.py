from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest

from app.limits import check_cooldown
from app.telegram import Notifier


def _get_csrf(client) -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    token = resp.cookies.get("csrf_token")
    assert token, "GET / must set csrf_token cookie"
    return token


def test_panel_index_renders_turkish_seed_data_dry_run_banner_and_security_headers(
    client,
) -> None:
    # Kabul: given freshly seeded DB in default dry_run mode, expect GET / returns 200,
    # Turkish HTML with lang="tr", seeded product names, dry_run banner, and CSP/security headers.
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'lang="tr"' in resp.text
    assert "Demo modu: Telegram mesajları gönderilmez (dry_run)" in resp.text
    assert "Filtre Kahve Makinesi 1.2L" in resp.text
    assert "Çelik Vakumlu Termos 750ml" in resp.text
    assert "Şarjlı LED Masa Lambası" in resp.text
    assert "Şimdi Kontrol Et" in resp.text

    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_panel_escapes_script_tags_in_product_name(client, db_conn) -> None:
    # Kabul: given a product whose name contains <script>alert(1)</script>,
    # expect GET / and GET /products/{id} escape HTML tags and never emit raw <script>.
    payload = "<script>alert(1)</script>"
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO products (sku, name, my_price, currency) "
            "VALUES ('SKU-XSS-1', %s, 100.00, 'TRY') RETURNING id",
            (payload,),
        )
        product_id = cur.fetchone()[0]
    db_conn.commit()

    index_resp = client.get("/")
    assert index_resp.status_code == 200
    assert payload not in index_resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in index_resp.text

    detail_resp = client.get(f"/products/{product_id}")
    assert detail_resp.status_code == 200
    assert payload not in detail_resp.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail_resp.text


def test_end_to_end_panel_mock_price_drop_triggers_dry_run_alert_on_panel(
    wired_client,
) -> None:
    # Kabul: given baseline check run, when cooldown is reset, mock store price is dropped via panel
    # below threshold (2400.00) with CSRF, and second check runs via panel, expect dry_run alert row
    # appears on GET / and zero outbound Telegram HTTP requests occur.
    transport_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        transport_calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    http_client = httpx.Client(transport=httpx.MockTransport(_handler))
    wired_client.app.state.notifier_override = Notifier(
        token="123:TEST", chat_id="-1001", dry_run=True, client=http_client
    )

    try:
        csrf = _get_csrf(wired_client)

        # Turn 1: baseline
        r1 = wired_client.post(
            "/panel/check",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert r1.status_code == 303

        # Reset cooldown before turn 2
        check_cooldown.reset()

        # Drop price on mock store item below 2400.00 threshold
        r_mock = wired_client.post(
            "/panel/mock/kahve-diyari-filtre-makine",
            data={"csrf_token": csrf, "price": "2299.00", "in_stock": "true"},
            follow_redirects=False,
        )
        assert r_mock.status_code == 303

        # Turn 2: detect price drop and log dry_run alert
        r2 = wired_client.post(
            "/panel/check",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert r2.status_code == 303

        page = wired_client.get("/")
        assert page.status_code == 200
        assert "dry_run" in page.text
        assert "2499.90" in page.text
        assert "2299.00" in page.text
        assert transport_calls == []
    finally:
        http_client.close()


def test_panel_check_cooldown_redirects_with_turkish_warning_and_shares_counter_with_api(
    wired_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given a check just ran via panel, expect immediate second POST /panel/check returns 303
    # (not 429) with a Turkish cooldown message on /, and POST /jobs/check returns 429 + Retry-After.
    monkeypatch.setenv("JOB_TOKEN", "shared-tok")
    csrf = _get_csrf(wired_client)

    first = wired_client.post(
        "/panel/check", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert first.status_code == 303

    second = wired_client.post(
        "/panel/check", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert second.status_code == 303
    followed = wired_client.get(second.headers["location"])
    assert followed.status_code == 200
    assert "soğuma" in followed.text.lower() or "bekleyin" in followed.text.lower()

    api_resp = wired_client.post(
        "/jobs/check", headers={"X-Job-Token": "shared-tok"}
    )
    assert api_resp.status_code == 429
    assert "retry-after" in api_resp.headers


def test_csrf_required_on_all_panel_post_endpoints(wired_client, db_conn) -> None:
    # Kabul: given all panel POST forms (check, mock, products, competitors, rules),
    # expect POST without CSRF token or with mismatched token returns 403, and valid token succeeds.
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY id LIMIT 1")
        product_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM competitors ORDER BY id LIMIT 1")
        competitor_id = cur.fetchone()[0]
    db_conn.commit()

    endpoints_and_payloads = [
        ("/panel/check", {}),
        (
            "/panel/mock/kahve-diyari-filtre-makine",
            {"price": "2450.00", "in_stock": "true"},
        ),
        (
            "/panel/products",
            {"name": "Yeni Ürün", "sku": "SKU-CSRF-1", "my_price": "199.90"},
        ),
        (
            "/panel/competitors",
            {
                "product_id": str(product_id),
                "store_name": "Test Mağaza",
                "url": "/mock-store/yeni-csrf-test",
                "parse_mode": "auto",
            },
        ),
        (
            "/panel/rules",
            {
                "competitor_id": str(competitor_id),
                "rule_type": "below_price",
                "threshold_value": "2000.00",
                "cooldown_min": "30",
            },
        ),
    ]

    # 1) No cookie, no form token -> 403
    for path, payload in endpoints_and_payloads:
        resp = wired_client.post(path, data=payload, follow_redirects=False)
        assert resp.status_code == 403, f"Expected 403 without CSRF on {path}"

    # 2) Cookie set, wrong form token -> 403
    csrf = _get_csrf(wired_client)
    for path, payload in endpoints_and_payloads:
        bad_payload = {**payload, "csrf_token": "wrong-csrf-token"}
        resp = wired_client.post(path, data=bad_payload, follow_redirects=False)
        assert resp.status_code == 403, f"Expected 403 with wrong CSRF on {path}"

    # 3) Valid CSRF token -> 303
    for path, payload in endpoints_and_payloads:
        ok_payload = {**payload, "csrf_token": csrf}
        resp = wired_client.post(path, data=ok_payload, follow_redirects=False)
        assert resp.status_code in (200, 303), f"Expected 303/200 with valid CSRF on {path}, got {resp.status_code}"


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://evil.com/x",
        "http://127.0.0.1:8000/admin",
        "file:///etc/passwd",
        "http://127.0.0.1:9999/mock-store/foo",
        "http://127.0.0.1:8000/mock-store/../admin",
    ],
)
def test_add_competitor_rejects_disallowed_urls_with_400(
    client, db_conn, bad_url: str
) -> None:
    # Kabul: given disallowed/SSRF competitor URL, expect POST /panel/competitors returns 400
    # and does not insert a row.
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY id LIMIT 1")
        product_id = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM competitors")
        before_count = cur.fetchone()[0]
    db_conn.commit()

    csrf = _get_csrf(client)
    resp = client.post(
        "/panel/competitors",
        data={
            "csrf_token": csrf,
            "product_id": str(product_id),
            "store_name": "Kötü Mağaza",
            "url": bad_url,
            "parse_mode": "auto",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM competitors")
        after_count = cur.fetchone()[0]
    db_conn.commit()
    assert after_count == before_count


def test_add_competitor_accepts_valid_mock_store_url(client, db_conn) -> None:
    # Kabul: given a valid /mock-store URL under demo_policy, expect POST /panel/competitors
    # returns 303 and persists the competitor.
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY id LIMIT 1")
        product_id = cur.fetchone()[0]
    db_conn.commit()

    csrf = _get_csrf(client)
    resp = client.post(
        "/panel/competitors",
        data={
            "csrf_token": csrf,
            "product_id": str(product_id),
            "store_name": "Geçerli Demo Mağaza",
            "url": "http://127.0.0.1:8000/mock-store/yeni-gecerli-urun",
            "parse_mode": "jsonld",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT store_name, parse_mode FROM competitors WHERE store_name = 'Geçerli Demo Mağaza'"
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row == ("Geçerli Demo Mağaza", "jsonld")


@pytest.mark.parametrize("bad_price", ["-10", "-0.01", "0", "0.00", "NaN", "Infinity", "1e9", "1000000.01", "abc"])
def test_mock_update_rejects_invalid_prices_with_400(
    client, db_conn, bad_price: str
) -> None:
    # Kabul: given negative, zero, NaN, Infinity, or >1,000,000 price on POST /panel/mock/{slug},
    # expect 400 with form error and original price preserved in DB.
    csrf = _get_csrf(client)
    resp = client.post(
        "/panel/mock/kahve-diyari-filtre-makine",
        data={"csrf_token": csrf, "price": bad_price, "in_stock": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 400

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT price FROM mock_store_items WHERE slug = 'kahve-diyari-filtre-makine'"
        )
        price = cur.fetchone()[0]
    db_conn.commit()
    assert price == Decimal("2499.90")


def test_product_detail_shows_price_history_and_inline_svg_and_404_for_missing(
    wired_client, db_conn
) -> None:
    # Kabul: given a product with price_history after checks, expect GET /products/{id} returns 200
    # with price_history rows and inline <svg> chart (no JS); expect unknown product id returns 404.
    csrf = _get_csrf(wired_client)

    # Run two checks with a price change to get 2 history points
    assert (
        wired_client.post(
            "/panel/check", data={"csrf_token": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    check_cooldown.reset()
    assert (
        wired_client.post(
            "/panel/mock/kahve-diyari-filtre-makine",
            data={"csrf_token": csrf, "price": "2350.00", "in_stock": "true"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        wired_client.post(
            "/panel/check", data={"csrf_token": csrf}, follow_redirects=False
        ).status_code
        == 303
    )

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products WHERE sku = 'SKU-KAHVE-001'")
        product_id = cur.fetchone()[0]
    db_conn.commit()

    resp = wired_client.get(f"/products/{product_id}")
    assert resp.status_code == 200
    assert "Filtre Kahve Makinesi 1.2L" in resp.text
    assert "2499.90" in resp.text
    assert "2350.00" in resp.text
    assert "<svg" in resp.text
    assert "<polyline" in resp.text
    assert not re.search(r"<script\b", resp.text, re.IGNORECASE)

    not_found = wired_client.get("/products/999999")
    assert not_found.status_code == 404


def test_product_and_rule_form_validation_and_static_css_without_cdn(
    client, db_conn
) -> None:
    # Kabul: given invalid product/rule form inputs (name > 100 chars, cooldown_min < 1, invalid rule_type,
    # missing mock slug), expect 400/404; expect /static/style.css returns 200 and panel has no external CDN links.
    csrf = _get_csrf(client)

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM competitors ORDER BY id LIMIT 1")
        competitor_id = cur.fetchone()[0]
    db_conn.commit()

    # Product name > 100 chars -> 400
    too_long = client.post(
        "/panel/products",
        data={"csrf_token": csrf, "name": "A" * 101, "sku": "SKU-LONG", "my_price": "100.00"},
        follow_redirects=False,
    )
    assert too_long.status_code == 400

    # Rule cooldown_min < 1 -> 400
    bad_cooldown = client.post(
        "/panel/rules",
        data={
            "csrf_token": csrf,
            "competitor_id": str(competitor_id),
            "rule_type": "stock_change",
            "threshold_value": "",
            "cooldown_min": "0",
        },
        follow_redirects=False,
    )
    assert bad_cooldown.status_code == 400

    # Valid stock_change rule without threshold -> 303
    ok_rule = client.post(
        "/panel/rules",
        data={
            "csrf_token": csrf,
            "competitor_id": str(competitor_id),
            "rule_type": "stock_change",
            "threshold_value": "",
            "cooldown_min": "15",
        },
        follow_redirects=False,
    )
    assert ok_rule.status_code == 303

    # Unknown mock slug -> 404
    unknown_mock = client.post(
        "/panel/mock/olmayan-urun-slug",
        data={"csrf_token": csrf, "price": "100.00", "in_stock": "true"},
        follow_redirects=False,
    )
    assert unknown_mock.status_code == 404

    # Static CSS served and no external CDN references in HTML
    css_resp = client.get("/static/style.css")
    assert css_resp.status_code == 200
    assert "text/css" in css_resp.headers["content-type"]

    index_html = client.get("/").text
    assert "https://" not in index_html
    assert "http://" not in index_html
