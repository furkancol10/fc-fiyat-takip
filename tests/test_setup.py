from __future__ import annotations

import os
import subprocess
import sys

import psycopg
import pytest

from tests.conftest import BASE_DATABASE_URL, _build_schema_url

SETUP_SCHEMA = "fiyattakip_setup_test"
TABLES = {
    "products", "competitors", "price_history",
    "alert_rules", "alert_logs", "mock_store_items",
}


@pytest.fixture()
def setup_url(monkeypatch):
    with psycopg.connect(BASE_DATABASE_URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SETUP_SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SETUP_SCHEMA}")
    url = _build_schema_url(BASE_DATABASE_URL, SETUP_SCHEMA)
    monkeypatch.setenv("DATABASE_URL", url)
    yield url
    with psycopg.connect(BASE_DATABASE_URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SETUP_SCHEMA} CASCADE")


def _q(url, sql):
    with psycopg.connect(url) as c:
        return c.execute(sql).fetchall()


def test_setup_empty_schema_creates_tables_and_seed(setup_url):
    from app.setup import main

    assert main([]) == 0
    names = {r[0] for r in _q(
        setup_url,
        f"SELECT table_name FROM information_schema.tables WHERE table_schema='{SETUP_SCHEMA}'",
    )}
    assert TABLES <= names
    assert len(names & TABLES) == 6
    skus = {r[0] for r in _q(setup_url, "SELECT sku FROM products")}
    assert {"SKU-KAHVE-001", "SKU-TERMOS-002", "SKU-LAMBA-003"} <= skus


def test_second_run_keeps_data(setup_url, capsys):
    from app.setup import main

    assert main([]) == 0
    with psycopg.connect(setup_url) as c:
        c.execute("UPDATE mock_store_items SET price = 1.00 WHERE slug='kamp-doga-celik-termos'")
    capsys.readouterr()
    assert main([]) == 0
    assert "zaten kurulu" in capsys.readouterr().out.lower()
    rows = _q(setup_url, "SELECT price FROM mock_store_items WHERE slug='kamp-doga-celik-termos'")
    assert float(rows[0][0]) == 1.0


def test_reset_restores_seed_price(setup_url):
    from app.setup import main

    assert main([]) == 0
    with psycopg.connect(setup_url) as c:
        c.execute("UPDATE mock_store_items SET price = 1.00 WHERE slug='kamp-doga-celik-termos'")
    assert main(["--reset"]) == 0
    rows = _q(setup_url, "SELECT price FROM mock_store_items WHERE slug='kamp-doga-celik-termos'")
    assert float(rows[0][0]) == 649.50


def test_missing_database_url_fails_without_password():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    r = subprocess.run(
        [sys.executable, "-m", "app.setup"], env=env, capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "DATABASE_URL" in (r.stdout + r.stderr)
    assert "fiyattakip:fiyattakip" not in r.stdout + r.stderr
