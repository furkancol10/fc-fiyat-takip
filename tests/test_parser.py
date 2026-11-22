from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.parser import ParseError, ParseResult, normalize_price, parse_product


def _wrap_jsonld(*blocks: str) -> str:
    scripts = "\n".join(
        f'<script type="application/ld+json">{block}</script>' for block in blocks
    )
    return f"<!doctype html><html><head>{scripts}</head><body><h1>Ürün</h1></body></html>"


def test_jsonld_full_uri_instock_and_outofstock() -> None:
    # Kabul: given JSON-LD Product/Offer with full schema.org availability URIs,
    # expect parse_product returns source=="jsonld" with exact price, currency and boolean in_stock
    in_stock_html = _wrap_jsonld(
        json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Filtre Kahve Makinesi",
                "offers": {
                    "@type": "Offer",
                    "price": "2499.90",
                    "priceCurrency": "TRY",
                    "availability": "https://schema.org/InStock",
                },
            }
        )
    )
    out_of_stock_html = _wrap_jsonld(
        json.dumps(
            {
                "@context": "http://schema.org",
                "@type": "Product",
                "name": "Çelik Termos",
                "offers": {
                    "@type": "Offer",
                    "price": "649.50",
                    "priceCurrency": "TRY",
                    "availability": "http://schema.org/OutOfStock",
                },
            }
        )
    )

    res_in = parse_product(in_stock_html)
    assert res_in == ParseResult(
        price=Decimal("2499.90"),
        currency="TRY",
        in_stock=True,
        source="jsonld",
    )

    res_out = parse_product(out_of_stock_html)
    assert res_out == ParseResult(
        price=Decimal("649.50"),
        currency="TRY",
        in_stock=False,
        source="jsonld",
    )


@pytest.mark.parametrize(
    ("availability", "expected_stock"),
    [
        ("InStock", True),
        ("https://schema.org/InStock", True),
        ("http://schema.org/InStock", True),
        ("OutOfStock", False),
        ("SoldOut", False),
        ("Discontinued", False),
        ("https://schema.org/SoldOut", False),
        ("http://schema.org/Discontinued", False),
        ("PreOrder", None),
        ("https://schema.org/BackOrder", None),
        ("UnknownStatus", None),
    ],
)
def test_jsonld_availability_variants(
    availability: str, expected_stock: bool | None
) -> None:
    # Kabul: given short-form, full-URI and unknown availability values in JSON-LD Offer,
    # expect in_stock normalized to True, False or None accordingly
    html = _wrap_jsonld(
        json.dumps(
            {
                "@type": "Product",
                "offers": {
                    "@type": "Offer",
                    "price": "199.90",
                    "priceCurrency": "TRY",
                    "availability": availability,
                },
            }
        )
    )
    result = parse_product(html)
    assert result.source == "jsonld"
    assert result.price == Decimal("199.90")
    assert result.in_stock is expected_stock


def test_jsonld_graph_and_array_structures() -> None:
    # Kabul: given JSON-LD using @graph or top-level JSON array containing a Product,
    # expect parse_product extracts Offer data from the Product node
    graph_html = _wrap_jsonld(
        json.dumps(
            {
                "@context": "https://schema.org",
                "@graph": [
                    {"@type": "WebSite", "name": "Demo"},
                    {
                        "@type": "Product",
                        "name": "Lamba",
                        "offers": {
                            "@type": "Offer",
                            "price": 589.9,
                            "priceCurrency": "TRY",
                            "availability": "InStock",
                        },
                    },
                ],
            }
        )
    )
    array_html = _wrap_jsonld(
        json.dumps(
            [
                {"@type": "Organization", "name": "Mağaza"},
                {
                    "@type": "Product",
                    "name": "Lamba",
                    "offers": {
                        "@type": "Offer",
                        "price": "450.00",
                        "priceCurrency": "USD",
                        "availability": "SoldOut",
                    },
                },
            ]
        )
    )

    res_graph = parse_product(graph_html)
    assert res_graph == ParseResult(
        price=Decimal("589.9"), currency="TRY", in_stock=True, source="jsonld"
    )

    res_array = parse_product(array_html)
    assert res_array == ParseResult(
        price=Decimal("450.00"), currency="USD", in_stock=False, source="jsonld"
    )


def test_jsonld_offers_list_picks_valid_offer() -> None:
    # Kabul: given Product.offers as a list of Offers, expect parse_product reads from Offer entries
    html = _wrap_jsonld(
        json.dumps(
            {
                "@type": "Product",
                "name": "Termos",
                "offers": [
                    {"@type": "Offer", "price": "", "availability": "OutOfStock"},
                    {
                        "@type": "Offer",
                        "price": "₺1.299,90",
                        "priceCurrency": "TRY",
                        "availability": "https://schema.org/InStock",
                    },
                ],
            }
        )
    )
    result = parse_product(html)
    assert result == ParseResult(
        price=Decimal("1299.90"), currency="TRY", in_stock=True, source="jsonld"
    )


def test_jsonld_malformed_block_does_not_block_valid_block() -> None:
    # Kabul: given a broken JSON-LD script followed by a valid Product JSON-LD script,
    # expect parse_product skips the broken block and parses the valid block
    html = _wrap_jsonld(
        '{"@type": "Product", "offers": {BROKEN JSON',
        json.dumps(
            {
                "@type": "Product",
                "offers": {
                    "@type": "Offer",
                    "price": "320.00",
                    "priceCurrency": "TRY",
                    "availability": "InStock",
                },
            }
        ),
    )
    result = parse_product(html)
    assert result == ParseResult(
        price=Decimal("320.00"), currency="TRY", in_stock=True, source="jsonld"
    )


def test_jsonld_ignores_product_level_price() -> None:
    # Kabul: given a Product with price at Product level instead of inside Offer,
    # expect parse_product ignores Product.price and raises ParseError
    html_no_offers = _wrap_jsonld(
        json.dumps(
            {
                "@type": "Product",
                "name": "Yanlış Şema",
                "price": "999.90",
                "priceCurrency": "TRY",
                "availability": "https://schema.org/InStock",
            }
        )
    )
    html_empty_offer = _wrap_jsonld(
        json.dumps(
            {
                "@type": "Product",
                "name": "Yanlış Şema",
                "price": "999.90",
                "offers": {"@type": "Offer", "availability": "InStock"},
            }
        )
    )

    with pytest.raises(ParseError):
        parse_product(html_no_offers, parse_mode="auto")

    with pytest.raises(ParseError):
        parse_product(html_empty_offer, parse_mode="jsonld")


def test_css_fallback_in_auto_mode_and_stock_labels() -> None:
    # Kabul: given HTML without JSON-LD containing default .price and .stock selectors,
    # expect auto mode falls back to CSS and extracts price, currency and stock status
    html_in = """
    <html><body>
      <span class="price">2549.00 TRY</span>
      <span class="stock">Stokta</span>
    </body></html>
    """
    html_out = """
    <html><body>
      <span class="price">₺1.299,90</span>
      <span class="stock">Tükendi</span>
    </body></html>
    """
    html_unknown = """
    <html><body>
      <div id="p">1299,90 TL</div>
      <div id="s">Ön Sipariş</div>
    </body></html>
    """

    res_in = parse_product(html_in, parse_mode="auto")
    assert res_in == ParseResult(
        price=Decimal("2549.00"), currency="TRY", in_stock=True, source="css"
    )

    res_out = parse_product(html_out, parse_mode="auto")
    assert res_out == ParseResult(
        price=Decimal("1299.90"), currency="TRY", in_stock=False, source="css"
    )

    res_unknown = parse_product(
        html_unknown,
        parse_mode="css",
        css_price_selector="#p",
        css_stock_selector="#s",
    )
    assert res_unknown == ParseResult(
        price=Decimal("1299.90"), currency="TRY", in_stock=None, source="css"
    )


def test_parse_mode_jsonld_raises_when_jsonld_missing() -> None:
    # Kabul: given CSS-only HTML and parse_mode="jsonld",
    # expect ParseError without falling back to CSS
    html = '<html><body><span class="price">500.00 TRY</span><span class="stock">Stokta</span></body></html>'
    with pytest.raises(ParseError):
        parse_product(html, parse_mode="jsonld")


def test_parse_mode_css_raises_when_selector_missing() -> None:
    # Kabul: given HTML missing the price CSS selector in parse_mode="css",
    # expect ParseError even if JSON-LD is present
    html = _wrap_jsonld(
        json.dumps(
            {
                "@type": "Product",
                "offers": {"@type": "Offer", "price": "500.00", "priceCurrency": "TRY"},
            }
        )
    )
    with pytest.raises(ParseError):
        parse_product(html, parse_mode="css")


@pytest.mark.parametrize(
    ("raw_price", "expected"),
    [
        ("1299.90", Decimal("1299.90")),
        (1299.9, Decimal("1299.9")),
        (1299, Decimal("1299")),
        ("₺1.299,90", Decimal("1299.90")),
        ("1,299.90", Decimal("1299.90")),
        ("1299,90 TL", Decimal("1299.90")),
        ("2549.00 TRY", Decimal("2549.00")),
        ("1 299,90 TL", Decimal("1299.90")),
        ("$49.95", Decimal("49.95")),
        ("€1.050,00", Decimal("1050.00")),
        ("12.345.678,90 TL", Decimal("12345678.90")),
    ],
)
def test_price_normalization_valid_formats(raw_price: Any, expected: Decimal) -> None:
    # Kabul: given diverse valid price representations (str/int/float, TR/US separators, symbols),
    # expect normalize_price returns the exact Decimal value
    assert normalize_price(raw_price) == expected


@pytest.mark.parametrize(
    "invalid_price",
    [
        0,
        0.0,
        "0",
        "0.00",
        "0,00 TL",
        -10,
        -0.01,
        "-1299.90",
        "-₺1.299,90",
        "₺-100",
        float("nan"),
        float("inf"),
        float("-inf"),
        "NaN",
        "Infinity",
        "Ücretsiz",
        "Fiyat sorunuz",
        "",
        "   ",
        None,
        True,
        False,
        "12.34.56",
    ],
)
def test_price_normalization_invalid_inputs_raise_parse_error(invalid_price: Any) -> None:
    # Kabul: given zero, negative, NaN/Inf, boolean, empty or non-numeric price values,
    # expect normalize_price raises ParseError
    with pytest.raises(ParseError):
        normalize_price(invalid_price)


def test_broken_html_raises_parse_error_and_does_not_fabricate_data() -> None:
    # Kabul: given truncated/broken HTML without JSON-LD or price selector,
    # expect parse_product raises ParseError instead of fabricating a result
    broken_html = """<!doctype html>
    <html lang="tr"><head><meta charset="utf-8"><title>Kırık</title></head>
    <body><div class="maintenance-notice"><h1>Ürün</h1>
    <p>Ürün bilgisi şu anda görüntülenemiyor<!-- eksik şablon bloğu
    </div>"""
    with pytest.raises(ParseError):
        parse_product(broken_html, parse_mode="auto")

    with pytest.raises(ParseError):
        parse_product("", parse_mode="auto")


@pytest.mark.parametrize("raw", ["١٢٣", "١,٢٣", "１２３", "1٢3"])
def test_non_ascii_digits_rejected(raw) -> None:
    # Kabul: given a price with non-ASCII (Arabic-Indic / fullwidth) digits, expect ParseError
    with pytest.raises(ParseError):
        normalize_price(raw)
