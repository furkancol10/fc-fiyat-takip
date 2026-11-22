from __future__ import annotations

import psycopg

from app.db import get_connection
from app.seed import seed_demo_data


def _reset_in_transaction(conn: psycopg.Connection) -> None:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE
                    alert_logs,
                    alert_rules,
                    price_history,
                    competitors,
                    products,
                    mock_store_items
                RESTART IDENTITY CASCADE
                """
            )
        seed_demo_data(conn)


def reset_demo(conn: psycopg.Connection | None = None) -> None:
    """Clear all demo tables and reload deterministic seed data in a single transaction."""
    if conn is not None:
        _reset_in_transaction(conn)
        return

    with get_connection() as owned_conn:
        _reset_in_transaction(owned_conn)


if __name__ == "__main__":
    reset_demo()
