from __future__ import annotations

import pytest

from app.limits import panel_rate_limiter


def _get_csrf(client) -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    token = resp.cookies.get("csrf_token")
    assert token
    return token


def test_product_and_competitor_quotas_enforced_and_freed_after_reset_demo(
    client, db_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given 3 seeded products and 6 seeded competitors, expect adding up to 10 products and
    # 20 competitors succeeds, the 11th product and 21st competitor return 400, and after
    # POST /jobs/reset-demo both quotas are free again.
    monkeypatch.setenv("JOB_TOKEN", "quota-tok")
    csrf = _get_csrf(client)

    # Seed has 3 products -> add 7 more to reach 10
    for idx in range(7):
        resp = client.post(
            "/panel/products",
            data={
                "csrf_token": csrf,
                "name": f"Ekstra Ürün {idx}",
                "sku": f"SKU-EXTRA-{idx}",
                "my_price": "150.00",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"Product {idx} failed with {resp.status_code}"

    # 11th product must fail with 400
    over_prod = client.post(
        "/panel/products",
        data={
            "csrf_token": csrf,
            "name": "Kota Aşan Ürün",
            "sku": "SKU-OVER-11",
            "my_price": "150.00",
        },
        follow_redirects=False,
    )
    assert over_prod.status_code == 400
    assert "kota" in over_prod.text.lower() or "10" in over_prod.text

    # Reset rate limiter so competitor quota loop doesn't hit 30/min IP limit
    panel_rate_limiter.reset()

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY id LIMIT 1")
        product_id = cur.fetchone()[0]
    db_conn.commit()

    # Seed has 6 competitors -> add 14 more to reach 20
    for idx in range(14):
        resp = client.post(
            "/panel/competitors",
            data={
                "csrf_token": csrf,
                "product_id": str(product_id),
                "store_name": f"Ekstra Mağaza {idx}",
                "url": f"/mock-store/ekstra-slug-{idx}",
                "parse_mode": "auto",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"Competitor {idx} failed with {resp.status_code}"

    # 21st competitor must fail with 400
    over_comp = client.post(
        "/panel/competitors",
        data={
            "csrf_token": csrf,
            "product_id": str(product_id),
            "store_name": "Kota Aşan Mağaza",
            "url": "/mock-store/ekstra-slug-overflow",
            "parse_mode": "auto",
        },
        follow_redirects=False,
    )
    assert over_comp.status_code == 400
    assert "kota" in over_comp.text.lower() or "20" in over_comp.text

    # Reset demo via jobs endpoint -> quotas should be free again
    reset_resp = client.post("/jobs/reset-demo", headers={"X-Job-Token": "quota-tok"})
    assert reset_resp.status_code == 200
    panel_rate_limiter.reset()

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM products ORDER BY id LIMIT 1")
        fresh_product_id = cur.fetchone()[0]
    db_conn.commit()

    after_reset_prod = client.post(
        "/panel/products",
        data={
            "csrf_token": csrf,
            "name": "Reset Sonrası Ürün",
            "sku": "SKU-AFTER-RESET",
            "my_price": "200.00",
        },
        follow_redirects=False,
    )
    assert after_reset_prod.status_code == 303

    after_reset_comp = client.post(
        "/panel/competitors",
        data={
            "csrf_token": csrf,
            "product_id": str(fresh_product_id),
            "store_name": "Reset Sonrası Mağaza",
            "url": "/mock-store/reset-sonrasi-slug",
            "parse_mode": "auto",
        },
        follow_redirects=False,
    )
    assert after_reset_comp.status_code == 303


def test_panel_post_rate_limit_blocks_31st_request_and_ignores_xff_by_default(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given default TRUST_PROXY (disabled), expect 30 panel POSTs succeed and 31st returns 429
    # even when rotating X-Forwarded-For headers on every request.
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    csrf = _get_csrf(client)

    for i in range(30):
        resp = client.post(
            "/panel/mock/kahve-diyari-filtre-makine",
            data={"csrf_token": csrf, "price": f"{2400 + i}.00", "in_stock": "true"},
            headers={"X-Forwarded-For": f"203.0.113.{i + 1}"},
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"Request #{i + 1} unexpectedly returned {resp.status_code}"

    blocked = client.post(
        "/panel/mock/kahve-diyari-filtre-makine",
        data={"csrf_token": csrf, "price": "2499.00", "in_stock": "true"},
        headers={"X-Forwarded-For": "198.51.100.99"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers


def test_panel_post_rate_limit_can_reset_and_respects_trust_proxy_when_enabled(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kabul: given TRUST_PROXY=1, expect different first X-Forwarded-For IPs have separate buckets
    # while the same X-Forwarded-For IP hits 429 on request 31; reset clears the counter.
    monkeypatch.setenv("TRUST_PROXY", "1")
    csrf = _get_csrf(client)

    for i in range(30):
        resp = client.post(
            "/panel/mock/kahve-diyari-filtre-makine",
            data={"csrf_token": csrf, "price": "2450.00", "in_stock": "true"},
            headers={"X-Forwarded-For": "198.51.100.10, 10.0.0.1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    # Same first IP -> 429
    same_ip = client.post(
        "/panel/mock/kahve-diyari-filtre-makine",
        data={"csrf_token": csrf, "price": "2450.00", "in_stock": "true"},
        headers={"X-Forwarded-For": "198.51.100.10, 10.0.0.2"},
        follow_redirects=False,
    )
    assert same_ip.status_code == 429

    # Different first IP -> allowed
    other_ip = client.post(
        "/panel/mock/kahve-diyari-filtre-makine",
        data={"csrf_token": csrf, "price": "2450.00", "in_stock": "true"},
        headers={"X-Forwarded-For": "198.51.100.11"},
        follow_redirects=False,
    )
    assert other_ip.status_code == 303

    # Reset clears limiter -> original IP allowed again
    panel_rate_limiter.reset()
    after_reset = client.post(
        "/panel/mock/kahve-diyari-filtre-makine",
        data={"csrf_token": csrf, "price": "2450.00", "in_stock": "true"},
        headers={"X-Forwarded-For": "198.51.100.10"},
        follow_redirects=False,
    )
    assert after_reset.status_code == 303
