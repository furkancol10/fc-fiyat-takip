from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from psycopg.rows import dict_row

from app.checker import run_check
from app.db import get_connection
from app.jobs import get_check_cooldown, get_fetch, get_notifier, get_now, get_policy
from app.limits import (
    CSRF_COOKIE_NAME,
    CSRF_FIELD_NAME,
    MAX_COMPETITORS,
    MAX_PRODUCTS,
    CheckCooldown,
    generate_csrf_token,
    get_client_ip,
    panel_rate_limiter,
    verify_csrf_token,
)
from app.security import DEFAULT_MOCK_BASE_URL, UrlNotAllowed, validate_url

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=True),
)

router = APIRouter(tags=["panel"])

MAX_FORM_BODY_BYTES = 64 * 1024
MIN_PRICE = Decimal("0.01")
MAX_PRICE = Decimal("1000000.00")
MAX_NAME_LEN = 100
MAX_SKU_LEN = 50
MAX_URL_LEN = 500
MAX_SELECTOR_LEN = 100
MAX_COOLDOWN_MIN = 43_200

VALID_PARSE_MODES = frozenset({"auto", "jsonld", "css"})
VALID_RULE_TYPES = frozenset({"below_price", "pct_drop", "stock_change"})

EVENT_TYPE_LABELS: dict[str, str] = {
    "below_price": "Fiyat eşik altı",
    "pct_drop": "Yüzde düşüş",
    "stock_change": "Stok değişimi",
}


async def parse_urlencoded_form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > MAX_FORM_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Form gövdesi çok büyük.")
    if not body:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items() if v}


def enforce_panel_post_guards(
    request: Request,
    form_data: dict[str, str] = Depends(parse_urlencoded_form),
) -> dict[str, str]:
    client_ip = get_client_ip(request)
    allowed, retry_after = panel_rate_limiter.check(client_ip)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Çok fazla istek gönderildi. Lütfen daha sonra tekrar deneyin.",
            headers={"Retry-After": str(retry_after)},
        )

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    form_token = form_data.get(CSRF_FIELD_NAME)
    if not verify_csrf_token(cookie_token, form_token):
        raise HTTPException(status_code=403, detail="CSRF doğrulaması başarısız.")

    return form_data


def _ensure_csrf_token(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing and len(existing) >= 16 and existing.isascii():
        return existing, False
    return generate_csrf_token(), True


def _attach_csrf_cookie(response: HTMLResponse, request: Request, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=(request.url.scheme == "https"),
        path="/",
    )


def _format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "Henüz kontrol edilmedi"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_decimal_range(
    raw: str | None,
    *,
    min_val: Decimal,
    max_val: Decimal,
    field_name: str,
) -> Decimal:
    text = (raw or "").strip()
    if not text:
        raise ValueError(f"{field_name} alanı zorunludur.")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} geçerli bir sayı olmalıdır.") from exc
    if not value.is_finite():
        raise ValueError(f"{field_name} sonlu bir sayı olmalıdır.")
    if value < min_val or value > max_val:
        raise ValueError(
            f"{field_name} {min_val:.2f} ile {max_val:.2f} arasında olmalıdır."
        )
    return value.quantize(Decimal("0.01"))


def _parse_bool(raw: str | None, *, field_name: str = "Stok durumu") -> bool:
    text = (raw or "").strip().lower()
    if text in {"true", "1", "on", "yes", "evet"}:
        return True
    if text in {"false", "0", "off", "no", "hayir", "hayır"}:
        return False
    raise ValueError(f"{field_name} geçersiz.")


def _derive_policy_base_url(policy: Any) -> str:
    hosts = list(getattr(policy, "allowed_hosts", ()))
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


def _validate_competitor_url(raw_url: str | None, policy: Any) -> str:
    cleaned = (raw_url or "").strip()
    if not cleaned:
        raise ValueError("Rakip URL alanı zorunludur.")
    if len(cleaned) > MAX_URL_LEN:
        raise ValueError(f"Rakip URL en fazla {MAX_URL_LEN} karakter olabilir.")

    is_relative = cleaned.startswith("/") and not cleaned.startswith("//")
    candidate = f"{_derive_policy_base_url(policy)}{cleaned}" if is_relative else cleaned

    try:
        validated = validate_url(
            candidate,
            allowed_hosts=policy.allowed_hosts,
            allowed_path_prefix=policy.allowed_path_prefix,
            strict_hosts=bool(getattr(policy, "strict", False)),
            strict_port=getattr(policy, "port", None),
        )
    except UrlNotAllowed as exc:
        raise ValueError(
            "Bu URL izin listesinde değil. Demoda yalnızca kendi /mock-store/ adresleri eklenebilir."
        ) from exc

    parsed_path = urlsplit(validated).path
    prefix = getattr(policy, "allowed_path_prefix", None)
    if prefix and parsed_path.rstrip("/") == prefix.rstrip("/"):
        raise ValueError("Geçerli bir /mock-store/<ürün> adresi girin.")

    return parsed_path if is_relative else validated


def _build_flash_message(request: Request) -> dict[str, str] | None:
    code = request.query_params.get("msg")
    if not code:
        return None

    def _safe_int(name: str) -> int:
        try:
            return max(0, int(request.query_params.get(name, "0")))
        except ValueError:
            return 0

    if code == "check_ok":
        checked = _safe_int("checked")
        changed = _safe_int("changed")
        alerts = _safe_int("alerts")
        errors = _safe_int("errors")
        return {
            "kind": "success",
            "text": (
                f"Kontrol tamamlandı: {checked} rakip tarandı, "
                f"{changed} değişim, {alerts} uyarı, {errors} hata."
            ),
        }
    if code == "check_cooldown":
        retry_after = max(1, _safe_int("retry_after"))
        return {
            "kind": "warning",
            "text": (
                f"Kontrol turu soğuma süresinde. Lütfen yaklaşık {retry_after} saniye bekleyin."
            ),
        }
    if code == "check_skipped":
        return {
            "kind": "warning",
            "text": "Başka bir kontrol turu şu anda çalışıyor; bu istek atlandı.",
        }
    if code == "mock_updated":
        return {"kind": "success", "text": "Sahte mağaza ürünü güncellendi."}
    if code == "product_added":
        return {"kind": "success", "text": "Yeni ürün eklendi."}
    if code == "competitor_added":
        return {"kind": "success", "text": "Yeni rakip eklendi."}
    if code == "rule_added":
        return {"kind": "success", "text": "Yeni uyarı kuralı eklendi."}
    return None


def _load_index_context(
    *,
    notifier: Any,
    csrf_token: str,
    flash: dict[str, str] | None = None,
    form_error: str | None = None,
) -> dict[str, Any]:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, sku, name, my_price, currency, created_at
                FROM products
                ORDER BY id ASC
                """
            )
            products = cur.fetchall()

            cur.execute(
                """
                SELECT
                    id, product_id, store_name, url, parse_mode,
                    last_price, last_in_stock, last_status,
                    consecutive_failures, last_checked_at, is_active
                FROM competitors
                ORDER BY id ASC
                """
            )
            competitors = cur.fetchall()

            cur.execute(
                """
                SELECT
                    al.id,
                    al.event_type,
                    al.old_val,
                    al.new_val,
                    al.status,
                    al.created_at,
                    p.name AS product_name,
                    c.store_name
                FROM alert_logs al
                JOIN alert_rules ar ON ar.id = al.rule_id
                JOIN competitors c ON c.id = ar.competitor_id
                JOIN products p ON p.id = c.product_id
                ORDER BY al.created_at DESC, al.id DESC
                LIMIT 20
                """
            )
            alert_logs = cur.fetchall()

            cur.execute(
                """
                SELECT slug, name, price, currency, in_stock, render_mode, updated_at
                FROM mock_store_items
                ORDER BY slug ASC
                """
            )
            mock_items = cur.fetchall()

    comps_by_product: dict[int, list[dict[str, Any]]] = {p["id"]: [] for p in products}
    product_price_map = {p["id"]: p["my_price"] for p in products}
    product_name_map = {p["id"]: p["name"] for p in products}

    all_competitors_view: list[dict[str, Any]] = []
    for comp in competitors:
        my_price = product_price_map.get(comp["product_id"])
        last_price = comp["last_price"]
        diff_pct_str = "—"
        diff_class = "diff-neutral"
        if last_price is not None and my_price is not None and my_price > 0:
            diff_pct = ((last_price - my_price) / my_price) * Decimal("100")
            diff_pct_str = f"{diff_pct:+.1f}%"
            if diff_pct < 0:
                diff_class = "diff-lower"
            elif diff_pct > 0:
                diff_class = "diff-higher"

        if comp["last_in_stock"] is True:
            stock_label = "Stokta"
            stock_class = "badge-in-stock"
        elif comp["last_in_stock"] is False:
            stock_label = "Tükendi"
            stock_class = "badge-out-of-stock"
        else:
            stock_label = "Bilinmiyor"
            stock_class = "badge-unknown"

        status_raw = comp["last_status"] or "bekliyor"
        failures = int(comp["consecutive_failures"] or 0)
        if status_raw == "ok":
            status_class = "badge-ok"
            status_text = "ok"
        elif status_raw == "bekliyor":
            status_class = "badge-unknown"
            status_text = "Bekliyor"
        else:
            status_class = "badge-error"
            status_text = f"{status_raw} ({failures} ardışık hata)"

        comp_view = {
            **comp,
            "product_name": product_name_map.get(comp["product_id"], ""),
            "last_price_fmt": f"{last_price:.2f}" if last_price is not None else "—",
            "diff_pct_str": diff_pct_str,
            "diff_class": diff_class,
            "stock_label": stock_label,
            "stock_class": stock_class,
            "status_text": status_text,
            "status_class": status_class,
            "last_checked_fmt": _format_dt(comp["last_checked_at"]),
        }
        comps_by_product.setdefault(comp["product_id"], []).append(comp_view)
        all_competitors_view.append(comp_view)

    products_view = [
        {
            **p,
            "my_price_fmt": f"{p['my_price']:.2f}",
            "competitors": comps_by_product.get(p["id"], []),
        }
        for p in products
    ]

    alerts_view = [
        {
            **al,
            "event_label": EVENT_TYPE_LABELS.get(al["event_type"], al["event_type"]),
            "created_at_fmt": _format_dt(al["created_at"]),
        }
        for al in alert_logs
    ]

    mock_items_view = [
        {
            **m,
            "price_fmt": f"{m['price']:.2f}",
        }
        for m in mock_items
    ]

    return {
        "csrf_token": csrf_token,
        "dry_run": bool(getattr(notifier, "dry_run", True)),
        "flash": flash,
        "form_error": form_error,
        "products": products_view,
        "all_competitors": all_competitors_view,
        "alert_logs": alerts_view,
        "mock_items": mock_items_view,
        "product_count": len(products),
        "max_products": MAX_PRODUCTS,
        "competitor_count": len(competitors),
        "max_competitors": MAX_COMPETITORS,
    }


def _render_index(
    request: Request,
    *,
    notifier: Any,
    status_code: int = 200,
    flash: dict[str, str] | None = None,
    form_error: str | None = None,
) -> HTMLResponse:
    csrf_token, should_set_cookie = _ensure_csrf_token(request)
    ctx = _load_index_context(
        notifier=notifier,
        csrf_token=csrf_token,
        flash=flash,
        form_error=form_error,
    )
    html = jinja_env.get_template("index.html").render(**ctx)
    response = HTMLResponse(content=html, status_code=status_code)
    if should_set_cookie:
        _attach_csrf_cookie(response, request, csrf_token)
    return response


def _build_svg_chart(history_desc: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not history_desc:
        return None
    chronological = list(reversed(history_desc))
    width = 480
    height = 160
    pad_x = 52
    pad_y = 20

    prices = [float(row["price"]) for row in chronological]
    min_p = min(prices)
    max_p = max(prices)
    span = max_p - min_p
    count = len(chronological)

    points: list[dict[str, str]] = []
    for idx, row in enumerate(chronological):
        if count == 1:
            x = width / 2.0
        else:
            x = pad_x + (idx / (count - 1)) * (width - 2 * pad_x)
        if span == 0:
            y = height / 2.0
        else:
            y = (height - pad_y) - ((float(row["price"]) - min_p) / span) * (height - 2 * pad_y)
        points.append(
            {
                "x": f"{x:.1f}",
                "y": f"{y:.1f}",
                "label": f"{row['checked_at_fmt']} — {row['price_fmt']}",
            }
        )

    polyline = " ".join(f"{pt['x']},{pt['y']}" for pt in points)
    return {
        "width": width,
        "height": height,
        "pad_x": pad_x,
        "pad_y": pad_y,
        "bottom_y": height - pad_y,
        "polyline": polyline,
        "points": points,
        "min_label": f"{min_p:.2f}",
        "max_label": f"{max_p:.2f}",
        "count": count,
    }


@router.get("/", response_class=HTMLResponse)
def panel_index(
    request: Request,
    notifier: Any = Depends(get_notifier),
) -> HTMLResponse:
    return _render_index(
        request,
        notifier=notifier,
        status_code=200,
        flash=_build_flash_message(request),
    )


@router.get("/products/{product_id}", response_class=HTMLResponse)
def panel_product_detail(
    product_id: int,
    request: Request,
    notifier: Any = Depends(get_notifier),
) -> HTMLResponse:
    csrf_token, should_set_cookie = _ensure_csrf_token(request)

    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, sku, name, my_price, currency, created_at
                FROM products
                WHERE id = %s
                """,
                (product_id,),
            )
            product = cur.fetchone()
            if product is None:
                html = jinja_env.get_template("error.html").render(
                    title="Ürün Bulunamadı",
                    message="İstenen ürün bulunamadı.",
                    dry_run=bool(getattr(notifier, "dry_run", True)),
                )
                return HTMLResponse(content=html, status_code=404)

            cur.execute(
                """
                SELECT
                    id, store_name, url, parse_mode,
                    last_price, last_in_stock, last_status,
                    consecutive_failures, last_checked_at
                FROM competitors
                WHERE product_id = %s
                ORDER BY id ASC
                """,
                (product_id,),
            )
            competitors = cur.fetchall()

            competitors_view: list[dict[str, Any]] = []
            for comp in competitors:
                cur.execute(
                    """
                    SELECT id, price, in_stock, checked_at
                    FROM price_history
                    WHERE competitor_id = %s
                    ORDER BY checked_at DESC, id DESC
                    LIMIT 30
                    """,
                    (comp["id"],),
                )
                raw_history = cur.fetchall()
                history = [
                    {
                        **row,
                        "price_fmt": f"{row['price']:.2f}",
                        "stock_label": (
                            "Stokta"
                            if row["in_stock"] is True
                            else ("Tükendi" if row["in_stock"] is False else "Bilinmiyor")
                        ),
                        "checked_at_fmt": _format_dt(row["checked_at"]),
                    }
                    for row in raw_history
                ]
                competitors_view.append(
                    {
                        **comp,
                        "last_price_fmt": (
                            f"{comp['last_price']:.2f}"
                            if comp["last_price"] is not None
                            else "—"
                        ),
                        "stock_label": (
                            "Stokta"
                            if comp["last_in_stock"] is True
                            else ("Tükendi" if comp["last_in_stock"] is False else "Bilinmiyor")
                        ),
                        "last_checked_fmt": _format_dt(comp["last_checked_at"]),
                        "history": history,
                        "chart": _build_svg_chart(history),
                    }
                )

    html = jinja_env.get_template("product_detail.html").render(
        product={**product, "my_price_fmt": f"{product['my_price']:.2f}"},
        competitors=competitors_view,
        dry_run=bool(getattr(notifier, "dry_run", True)),
        csrf_token=csrf_token,
    )
    response = HTMLResponse(content=html, status_code=200)
    if should_set_cookie:
        _attach_csrf_cookie(response, request, csrf_token)
    return response


@router.post("/panel/check")
def panel_check(
    _form: dict[str, str] = Depends(enforce_panel_post_guards),
    fetch: Callable[..., str] = Depends(get_fetch),
    notifier: Any = Depends(get_notifier),
    policy: Any = Depends(get_policy),
    now: datetime | None = Depends(get_now),
    cooldown: CheckCooldown = Depends(get_check_cooldown),
) -> RedirectResponse:
    allowed, retry_after = cooldown.try_acquire()
    if not allowed:
        query = urlencode({"msg": "check_cooldown", "retry_after": retry_after})
        return RedirectResponse(url=f"/?{query}", status_code=303)

    with get_connection() as conn:
        summary = run_check(
            conn,
            fetch=fetch,
            notifier=notifier,
            policy=policy,
            now=now,
        )

    if summary.skipped:
        return RedirectResponse(url="/?msg=check_skipped", status_code=303)

    query = urlencode(
        {
            "msg": "check_ok",
            "checked": summary.checked,
            "changed": summary.changed,
            "alerts": summary.alerts_sent + summary.alerts_dry_run,
            "errors": summary.errors,
        }
    )
    return RedirectResponse(url=f"/?{query}", status_code=303)


@router.post("/panel/mock/{slug}")
def panel_update_mock_item(
    slug: str,
    request: Request,
    form: dict[str, str] = Depends(enforce_panel_post_guards),
    notifier: Any = Depends(get_notifier),
):
    try:
        price = _parse_decimal_range(
            form.get("price"),
            min_val=MIN_PRICE,
            max_val=MAX_PRICE,
            field_name="Fiyat",
        )
        in_stock = _parse_bool(form.get("in_stock"), field_name="Stok durumu")
    except ValueError as exc:
        return _render_index(request, notifier=notifier, status_code=400, form_error=str(exc))

    with get_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mock_store_items
                    SET price = %s, in_stock = %s, updated_at = NOW()
                    WHERE slug = %s
                    RETURNING slug
                    """,
                    (price, in_stock, slug),
                )
                updated = cur.fetchone()

    if updated is None:
        raise HTTPException(status_code=404, detail="Sahte mağaza ürünü bulunamadı.")

    return RedirectResponse(url="/?msg=mock_updated", status_code=303)


@router.post("/panel/products")
def panel_add_product(
    request: Request,
    form: dict[str, str] = Depends(enforce_panel_post_guards),
    notifier: Any = Depends(get_notifier),
):
    name = (form.get("name") or "").strip()
    sku = (form.get("sku") or "").strip()

    try:
        if not name or len(name) > MAX_NAME_LEN:
            raise ValueError(f"Ürün adı 1 ile {MAX_NAME_LEN} karakter arasında olmalıdır.")
        if not sku or len(sku) > MAX_SKU_LEN:
            raise ValueError(f"SKU 1 ile {MAX_SKU_LEN} karakter arasında olmalıdır.")
        my_price = _parse_decimal_range(
            form.get("my_price"),
            min_val=MIN_PRICE,
            max_val=MAX_PRICE,
            field_name="Benim fiyatım",
        )
    except ValueError as exc:
        return _render_index(request, notifier=notifier, status_code=400, form_error=str(exc))

    try:
        with get_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM products")
                    total = cur.fetchone()[0]
                    if total >= MAX_PRODUCTS:
                        return _render_index(
                            request,
                            notifier=notifier,
                            status_code=400,
                            form_error=f"Ürün kotası doldu (en fazla {MAX_PRODUCTS} ürün eklenebilir).",
                        )
                    cur.execute(
                        """
                        INSERT INTO products (sku, name, my_price, currency)
                        VALUES (%s, %s, %s, 'TRY')
                        """,
                        (sku, name, my_price),
                    )
    except psycopg.errors.UniqueViolation:
        return _render_index(
            request,
            notifier=notifier,
            status_code=400,
            form_error="Bu SKU zaten kayıtlı.",
        )

    return RedirectResponse(url="/?msg=product_added", status_code=303)


@router.post("/panel/competitors")
def panel_add_competitor(
    request: Request,
    form: dict[str, str] = Depends(enforce_panel_post_guards),
    notifier: Any = Depends(get_notifier),
    policy: Any = Depends(get_policy),
):
    store_name = (form.get("store_name") or "").strip()
    parse_mode = (form.get("parse_mode") or "auto").strip().lower()
    css_price_selector = (form.get("css_price_selector") or ".price").strip() or ".price"
    css_stock_selector = (form.get("css_stock_selector") or ".stock").strip() or ".stock"

    try:
        try:
            product_id = int((form.get("product_id") or "").strip())
        except ValueError as exc:
            raise ValueError("Geçerli bir ürün seçin.") from exc
        if product_id <= 0:
            raise ValueError("Geçerli bir ürün seçin.")
        if not store_name or len(store_name) > MAX_NAME_LEN:
            raise ValueError(f"Mağaza adı 1 ile {MAX_NAME_LEN} karakter arasında olmalıdır.")
        if parse_mode not in VALID_PARSE_MODES:
            raise ValueError("Geçersiz ayrıştırma modu (auto, jsonld veya css seçin).")
        if len(css_price_selector) > MAX_SELECTOR_LEN or len(css_stock_selector) > MAX_SELECTOR_LEN:
            raise ValueError(f"CSS seçicisi en fazla {MAX_SELECTOR_LEN} karakter olabilir.")
        validated_url = _validate_competitor_url(form.get("url"), policy)
    except ValueError as exc:
        return _render_index(request, notifier=notifier, status_code=400, form_error=str(exc))

    try:
        with get_connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM competitors")
                    total = cur.fetchone()[0]
                    if total >= MAX_COMPETITORS:
                        return _render_index(
                            request,
                            notifier=notifier,
                            status_code=400,
                            form_error=f"Rakip kotası doldu (en fazla {MAX_COMPETITORS} rakip eklenebilir).",
                        )
                    cur.execute("SELECT 1 FROM products WHERE id = %s", (product_id,))
                    if cur.fetchone() is None:
                        return _render_index(
                            request,
                            notifier=notifier,
                            status_code=400,
                            form_error="Seçilen ürün bulunamadı.",
                        )
                    cur.execute(
                        """
                        INSERT INTO competitors (
                            product_id, store_name, url, parse_mode,
                            css_price_selector, css_stock_selector
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            product_id,
                            store_name,
                            validated_url,
                            parse_mode,
                            css_price_selector,
                            css_stock_selector,
                        ),
                    )
    except psycopg.errors.UniqueViolation:
        return _render_index(
            request,
            notifier=notifier,
            status_code=400,
            form_error="Bu ürün için bu rakip URL zaten kayıtlı.",
        )

    return RedirectResponse(url="/?msg=competitor_added", status_code=303)


@router.post("/panel/rules")
def panel_add_rule(
    request: Request,
    form: dict[str, str] = Depends(enforce_panel_post_guards),
    notifier: Any = Depends(get_notifier),
):
    rule_type = (form.get("rule_type") or "").strip().lower()

    try:
        try:
            competitor_id = int((form.get("competitor_id") or "").strip())
        except ValueError as exc:
            raise ValueError("Geçerli bir rakip seçin.") from exc
        if competitor_id <= 0:
            raise ValueError("Geçerli bir rakip seçin.")
        if rule_type not in VALID_RULE_TYPES:
            raise ValueError("Geçersiz kural türü.")

        try:
            cooldown_min = int((form.get("cooldown_min") or "60").strip())
        except ValueError as exc:
            raise ValueError("Bekleme süresi (dk) tam sayı olmalıdır.") from exc
        if cooldown_min < 1 or cooldown_min > MAX_COOLDOWN_MIN:
            raise ValueError(f"Bekleme süresi 1 ile {MAX_COOLDOWN_MIN} dakika arasında olmalıdır.")

        threshold_value: Decimal | None = None
        if rule_type == "below_price":
            threshold_value = _parse_decimal_range(
                form.get("threshold_value"),
                min_val=MIN_PRICE,
                max_val=MAX_PRICE,
                field_name="Eşik fiyatı",
            )
        elif rule_type == "pct_drop":
            threshold_value = _parse_decimal_range(
                form.get("threshold_value"),
                min_val=Decimal("0.01"),
                max_val=Decimal("100.00"),
                field_name="Yüzde düşüş eşiği",
            )
    except ValueError as exc:
        return _render_index(request, notifier=notifier, status_code=400, form_error=str(exc))

    with get_connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM competitors WHERE id = %s", (competitor_id,))
                if cur.fetchone() is None:
                    return _render_index(
                        request,
                        notifier=notifier,
                        status_code=400,
                        form_error="Seçilen rakip bulunamadı.",
                    )
                cur.execute(
                    """
                    INSERT INTO alert_rules (
                        competitor_id, rule_type, threshold_value, cooldown_min
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (competitor_id, rule_type, threshold_value, cooldown_min),
                )

    return RedirectResponse(url="/?msg=rule_added", status_code=303)
