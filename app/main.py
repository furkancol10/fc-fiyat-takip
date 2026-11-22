from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from psycopg.rows import dict_row

from app.db import get_connection

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
)

app = FastAPI(title="fc-fiyat-takip")


def _safe_jsonld_dumps(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    return raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _format_price(price: Decimal) -> str:
    return f"{price:.2f}"


def _render_mock_store_page(item: dict[str, Any]) -> str:
    render_mode = item["render_mode"]
    price_str = _format_price(item["price"])
    stock_label = "Stokta" if item["in_stock"] else "Tükendi"

    context: dict[str, Any] = {
        "name": item["name"],
        "price_str": price_str,
        "currency": item["currency"],
        "in_stock": item["in_stock"],
        "stock_label": stock_label,
    }

    if render_mode == "jsonld":
        availability_uri = (
            "https://schema.org/InStock"
            if item["in_stock"]
            else "https://schema.org/OutOfStock"
        )
        jsonld_payload = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": item["name"],
            "offers": {
                "@type": "Offer",
                "price": price_str,
                "priceCurrency": item["currency"],
                "availability": availability_uri,
            },
        }
        context["jsonld_safe"] = _safe_jsonld_dumps(jsonld_payload)
        template = jinja_env.get_template("mock_store_jsonld.html")
    elif render_mode == "css_only":
        template = jinja_env.get_template("mock_store_css_only.html")
    elif render_mode == "broken":
        template = jinja_env.get_template("mock_store_broken.html")
    else:
        raise HTTPException(status_code=500, detail="Unsupported render mode")

    return template.render(**context)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {"status": "ok"}


@app.get("/mock-store/{slug}", response_class=HTMLResponse)
def mock_store_item(slug: str) -> HTMLResponse:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT slug, name, price, currency, in_stock, render_mode
                FROM mock_store_items
                WHERE slug = %s
                """,
                (slug,),
            )
            item = cur.fetchone()

    if item is None:
        raise HTTPException(status_code=404, detail="Product not found")

    html = _render_mock_store_page(item)
    return HTMLResponse(content=html, status_code=200)
