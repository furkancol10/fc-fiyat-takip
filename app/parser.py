from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

DEFAULT_CSS_PRICE_SELECTOR = ".price"
DEFAULT_CSS_STOCK_SELECTOR = ".stock"

_CURRENCY_SYMBOL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"₺|\b(?:TRY|TL)\b", re.IGNORECASE), "TRY"),
    (re.compile(r"\$|\bUSD\b", re.IGNORECASE), "USD"),
    (re.compile(r"€|\bEUR\b", re.IGNORECASE), "EUR"),
    (re.compile(r"£|\bGBP\b", re.IGNORECASE), "GBP"),
]

_STRIP_CURRENCY_RE = re.compile(
    r"(?:₺|\$|€|£|\b(?:TRY|TL|USD|EUR|GBP)\b)",
    re.IGNORECASE,
)

_NEGATIVE_PRICE_RE = re.compile(r"-\s*(?:[₺$€£]\s*)?\d")

_IN_STOCK_TOKENS = frozenset({"instock"})
_OUT_OF_STOCK_TOKENS = frozenset({"outofstock", "soldout", "discontinued"})

_CSS_OUT_OF_STOCK_PHRASES = (
    "tükendi",
    "tukendi",
    "stokta yok",
    "stok yok",
    "out of stock",
    "outofstock",
    "sold out",
    "soldout",
    "discontinued",
    "unavailable",
)
_CSS_IN_STOCK_PHRASES = (
    "stokta",
    "in stock",
    "instock",
    "available",
    "mevcut",
)


class ParseError(Exception):
    """Raised when product price/stock data cannot be parsed from HTML."""


@dataclass(frozen=True)
class ParseResult:
    price: Decimal
    currency: str | None
    in_stock: bool | None
    source: Literal["jsonld", "css"]


def _turkish_lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


def _extract_currency_from_text(text: str) -> str | None:
    for pattern, code in _CURRENCY_SYMBOL_PATTERNS:
        if pattern.search(text):
            return code
    return None


def normalize_price(raw: Any) -> Decimal:
    """Normalize a raw price (str/int/float/Decimal) into a positive finite Decimal."""
    if isinstance(raw, bool) or raw is None:
        raise ParseError(f"Invalid price value: {raw!r}")

    if isinstance(raw, Decimal):
        if not raw.is_finite() or raw <= 0:
            raise ParseError(f"Non-positive or non-finite price: {raw!r}")
        return raw

    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ParseError(f"Non-finite price: {raw!r}")
        value = Decimal(str(raw))
        if not value.is_finite() or value <= 0:
            raise ParseError(f"Non-positive price: {raw!r}")
        return value

    if not isinstance(raw, str):
        raise ParseError(f"Unsupported price type: {type(raw).__name__}")

    text = raw.strip()
    if not text:
        raise ParseError("Empty price string")

    if _NEGATIVE_PRICE_RE.search(text):
        raise ParseError(f"Negative price: {raw!r}")

    cleaned = _STRIP_CURRENCY_RE.sub("", text)
    cleaned = cleaned.replace("\u00a0", " ").replace("\u202f", " ").strip()
    if cleaned.startswith("+"):
        cleaned = cleaned[1:].strip()

    if not cleaned or not cleaned.isascii() or not re.fullmatch(r"[\d., ]+", cleaned):
        raise ParseError(f"Unparseable price string: {raw!r}")

    if " " in cleaned:
        if not re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", cleaned):
            raise ParseError(f"Invalid space grouping in price: {raw!r}")
        cleaned = cleaned.replace(" ", "")

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    if has_dot and has_comma:
        if cleaned.rfind(",") > cleaned.rfind("."):
            if not re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d+", cleaned):
                raise ParseError(f"Invalid TR/EU price format: {raw!r}")
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})*\.\d+", cleaned):
                raise ParseError(f"Invalid US/UK price format: {raw!r}")
            normalized = cleaned.replace(",", "")
    elif has_comma:
        if cleaned.count(",") > 1:
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
                raise ParseError(f"Invalid comma grouping in price: {raw!r}")
            normalized = cleaned.replace(",", "")
        else:
            if not re.fullmatch(r"\d+,\d+", cleaned):
                raise ParseError(f"Invalid comma price format: {raw!r}")
            if re.fullmatch(r"[1-9]\d{0,2},\d{3}", cleaned):
                normalized = cleaned.replace(",", "")
            else:
                normalized = cleaned.replace(",", ".")
    elif has_dot:
        if cleaned.count(".") > 1:
            if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
                raise ParseError(f"Invalid dot grouping in price: {raw!r}")
            normalized = cleaned.replace(".", "")
        else:
            if not re.fullmatch(r"\d+\.\d+", cleaned):
                raise ParseError(f"Invalid dot price format: {raw!r}")
            if re.fullmatch(r"[1-9]\d{0,2}\.\d{3}", cleaned):
                normalized = cleaned.replace(".", "")
            else:
                normalized = cleaned
    else:
        if not re.fullmatch(r"\d+", cleaned):
            raise ParseError(f"Invalid integer price format: {raw!r}")
        normalized = cleaned

    try:
        value = Decimal(normalized)
    except InvalidOperation as exc:
        raise ParseError(f"Cannot convert price to Decimal: {raw!r}") from exc

    if not value.is_finite() or value <= 0:
        raise ParseError(f"Non-positive or non-finite price: {raw!r}")

    return value


def _has_type(node: dict[str, Any], expected: str) -> bool:
    raw_type = node.get("@type")
    if isinstance(raw_type, str):
        types = [raw_type]
    elif isinstance(raw_type, list):
        types = [t for t in raw_type if isinstance(t, str)]
    else:
        return False

    target = expected.lower()
    for item in types:
        token = item.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].lower()
        if token == target:
            return True
    return False


def _iter_product_nodes(data: Any) -> Iterator[dict[str, Any]]:
    if isinstance(data, list):
        for element in data:
            yield from _iter_product_nodes(element)
    elif isinstance(data, dict):
        if _has_type(data, "Product"):
            yield data
        if "@graph" in data:
            yield from _iter_product_nodes(data["@graph"])


def _iter_offers(offers_field: Any) -> Iterator[dict[str, Any]]:
    if isinstance(offers_field, list):
        for item in offers_field:
            yield from _iter_offers(item)
    elif isinstance(offers_field, dict):
        if _has_type(offers_field, "AggregateOffer") and "offers" in offers_field:
            yield from _iter_offers(offers_field["offers"])
            return
        if "@type" in offers_field and not _has_type(offers_field, "Offer"):
            return
        yield offers_field


def _normalize_availability(raw: Any) -> bool | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    if "://" in text:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        if host not in {"schema.org", "www.schema.org"}:
            return None
        token = parts.path.strip("/")
    elif text.lower().startswith("schema:"):
        token = text.split(":", 1)[1].strip()
    else:
        token = text

    token_lower = token.lower()
    if token_lower in _IN_STOCK_TOKENS:
        return True
    if token_lower in _OUT_OF_STOCK_TOKENS:
        return False
    return None


def _parse_jsonld(soup: BeautifulSoup) -> ParseResult | None:
    for script in soup.find_all("script"):
        raw_type = script.get("type")
        if not isinstance(raw_type, str):
            continue
        media_type = raw_type.split(";", 1)[0].strip().lower()
        if media_type != "application/ld+json":
            continue

        raw_json = script.string if script.string is not None else script.get_text()
        if not raw_json or not raw_json.strip():
            continue

        try:
            payload = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

        for product in _iter_product_nodes(payload):
            for offer in _iter_offers(product.get("offers")):
                if "price" not in offer:
                    continue
                try:
                    price = normalize_price(offer["price"])
                except ParseError:
                    continue

                raw_currency = offer.get("priceCurrency")
                currency = (
                    raw_currency.strip()
                    if isinstance(raw_currency, str) and raw_currency.strip()
                    else None
                )
                in_stock = _normalize_availability(offer.get("availability"))
                return ParseResult(
                    price=price,
                    currency=currency,
                    in_stock=in_stock,
                    source="jsonld",
                )

    return None


def _parse_stock_text(text: str) -> bool | None:
    lowered = _turkish_lower(text).strip()
    if not lowered:
        return None
    for phrase in _CSS_OUT_OF_STOCK_PHRASES:
        if phrase in lowered:
            return False
    for phrase in _CSS_IN_STOCK_PHRASES:
        if phrase in lowered:
            return True
    return None


def _parse_css(
    soup: BeautifulSoup,
    css_price_selector: str | None,
    css_stock_selector: str | None,
) -> ParseResult:
    price_selector = css_price_selector or DEFAULT_CSS_PRICE_SELECTOR
    stock_selector = css_stock_selector or DEFAULT_CSS_STOCK_SELECTOR

    try:
        price_el = soup.select_one(price_selector)
    except Exception as exc:
        raise ParseError(f"Invalid CSS price selector: {price_selector!r}") from exc

    if price_el is None:
        raise ParseError(f"CSS price selector not found: {price_selector!r}")

    price_text = price_el.get_text(" ", strip=True)
    price = normalize_price(price_text)
    currency = _extract_currency_from_text(price_text)

    try:
        stock_el = soup.select_one(stock_selector)
    except Exception as exc:
        raise ParseError(f"Invalid CSS stock selector: {stock_selector!r}") from exc

    in_stock = (
        _parse_stock_text(stock_el.get_text(" ", strip=True))
        if stock_el is not None
        else None
    )

    return ParseResult(
        price=price,
        currency=currency,
        in_stock=in_stock,
        source="css",
    )


def parse_product(
    html: str,
    parse_mode: str = "auto",
    css_price_selector: str | None = None,
    css_stock_selector: str | None = None,
) -> ParseResult:
    """Parse product price, currency and stock status from raw HTML."""
    if not isinstance(html, str) or not html.strip():
        raise ParseError("Empty HTML content")

    if parse_mode not in {"auto", "jsonld", "css"}:
        raise ParseError(f"Unsupported parse_mode: {parse_mode!r}")

    soup = BeautifulSoup(html, "html.parser")

    if parse_mode == "jsonld":
        result = _parse_jsonld(soup)
        if result is None:
            raise ParseError("No valid JSON-LD Product Offer found")
        return result

    if parse_mode == "css":
        return _parse_css(soup, css_price_selector, css_stock_selector)

    result = _parse_jsonld(soup)
    if result is not None:
        return result
    return _parse_css(soup, css_price_selector, css_stock_selector)
