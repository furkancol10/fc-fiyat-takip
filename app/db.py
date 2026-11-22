from __future__ import annotations

import os
from pathlib import Path

import psycopg

DEFAULT_DATABASE_URL = "postgresql://fiyattakip:fiyattakip@localhost:5435/fiyattakip"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_connection(database_url: str | None = None) -> psycopg.Connection:
    url = database_url or get_database_url()
    return psycopg.connect(url)


def apply_schema(conn: psycopg.Connection) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
