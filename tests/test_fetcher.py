from __future__ import annotations

import httpx
import pytest

from app.fetcher import MAX_BODY_BYTES, FetchError, fetch_html
from app.parser import ParseError, parse_product
from app.security import UrlNotAllowed, UrlPolicy, demo_policy


def _demo_test_policy() -> UrlPolicy:
    return demo_policy("http://127.0.0.1:8000")


def test_exception_classes_are_distinct() -> None:
    # Kabul: given FetchError and UrlNotAllowed classes,
    # expect neither is a subclass of the other
    assert not issubclass(FetchError, UrlNotAllowed)
    assert not issubclass(UrlNotAllowed, FetchError)


def test_5xx_retries_twice_then_raises_fetch_error() -> None:
    # Kabul: given a server always returning 503, expect fetch_html makes exactly 3 calls
    # (1 initial + 2 retries) with exponential backoff sleeps and raises FetchError
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="Service Unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/item",
                client=client,
                policy=_demo_test_policy(),
                sleep=sleeps.append,
            )

    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_5xx_recovers_on_retry() -> None:
    # Kabul: given a 502 on first attempt and 200 text/html on second attempt,
    # expect fetch_html retries once and returns the HTML body
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(
            200,
            text="<html><body>OK</body></html>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        html = fetch_html(
            "http://127.0.0.1:8000/mock-store/item",
            client=client,
            policy=_demo_test_policy(),
            sleep=sleeps.append,
        )

    assert html == "<html><body>OK</body></html>"
    assert calls == 2
    assert sleeps == [0.5]


def test_4xx_does_not_retry() -> None:
    # Kabul: given a 404 response, expect fetch_html makes exactly 1 call with no sleep and raises FetchError
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, text="Not Found")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/missing",
                client=client,
                policy=_demo_test_policy(),
                sleep=sleeps.append,
            )

    assert calls == 1
    assert sleeps == []


def test_timeout_retries_twice_then_raises_fetch_error() -> None:
    # Kabul: given httpx.ReadTimeout on every request, expect fetch_html retries twice
    # (total 3 calls) using injected sleep and raises FetchError
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/slow",
                client=client,
                policy=_demo_test_policy(),
                sleep=sleeps.append,
            )

    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_3xx_redirect_is_not_followed_and_raises_fetch_error() -> None:
    # Kabul: given a 302 redirect to an internal metadata URL, expect fetch_html does NOT follow
    # the redirect (exactly 1 call) and raises FetchError
    requested_urls: list[str] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data/"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/redir",
                client=client,
                policy=_demo_test_policy(),
                sleep=sleeps.append,
            )

    assert requested_urls == ["http://127.0.0.1:8000/mock-store/redir"]
    assert sleeps == []


def test_body_over_1mb_rejected_and_exact_1mb_accepted() -> None:
    # Kabul: given a response body larger than 1 MB (chunked or with Content-Length),
    # expect FetchError; given a body of exactly 1 MB, expect success
    oversized = b"a" * (MAX_BODY_BYTES + 1)
    exact = b"b" * MAX_BODY_BYTES

    def oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=oversized,
            headers={"content-type": "text/html"},
        )

    class _ChunkedStream(httpx.SyncByteStream):
        def __iter__(self):
            chunk = b"x" * (256 * 1024)
            for _ in range(5):  # 1.25 MB without Content-Length
                yield chunk

    def chunked_oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_ChunkedStream(),
            headers={"content-type": "text/html"},
        )

    def exact_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=exact,
            headers={"content-type": "application/xhtml+xml; charset=utf-8"},
        )

    with httpx.Client(transport=httpx.MockTransport(oversized_handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/big",
                client=client,
                policy=_demo_test_policy(),
                sleep=lambda _: None,
            )

    with httpx.Client(transport=httpx.MockTransport(chunked_oversized_handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/chunked-big",
                client=client,
                policy=_demo_test_policy(),
                sleep=lambda _: None,
            )

    with httpx.Client(transport=httpx.MockTransport(exact_handler)) as client:
        body = fetch_html(
            "http://127.0.0.1:8000/mock-store/exact",
            client=client,
            policy=_demo_test_policy(),
            sleep=lambda _: None,
        )
    assert len(body) == MAX_BODY_BYTES


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain", "application/octet-stream", ""],
)
def test_disallowed_content_type_rejected(content_type: str) -> None:
    # Kabul: given a 200 response whose Content-Type is not text/html or application/xhtml+xml,
    # expect fetch_html makes 1 call and raises FetchError
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {"content-type": content_type} if content_type else {}
        return httpx.Response(200, text="<html></html>", headers=headers)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FetchError):
            fetch_html(
                "http://127.0.0.1:8000/mock-store/not-html",
                client=client,
                policy=_demo_test_policy(),
                sleep=lambda _: None,
            )

    assert calls == 1


def test_disallowed_url_never_hits_network() -> None:
    # Kabul: given a URL blocked by security policy, expect fetch_html raises UrlNotAllowed
    # with zero HTTP calls made to the transport
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UrlNotAllowed):
            fetch_html(
                "http://169.254.169.254/latest/meta-data/",
                client=client,
                policy=_demo_test_policy(),
                sleep=lambda _: None,
            )

        with pytest.raises(UrlNotAllowed):
            fetch_html(
                "http://127.0.0.1:8000/healthz",
                client=client,
                policy=_demo_test_policy(),
                sleep=lambda _: None,
            )

    assert calls == 0


def test_integration_fetch_and_parse_mock_store_pages(client, db_conn) -> None:
    # Kabul: given the real FastAPI app and seeded PostgreSQL mock_store_items (no mocks),
    # expect fetch_html + parse_product yields source=="jsonld" for jsonld items,
    # source=="css" for css_only items (matching exact seeded price and stock),
    # and raises ParseError for broken items
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT slug, price, currency, in_stock, render_mode "
            "FROM mock_store_items ORDER BY slug"
        )
        rows = cur.fetchall()

    assert len(rows) >= 6
    policy = demo_policy(str(client.base_url))

    seen_modes: set[str] = set()
    for slug, expected_price, expected_currency, expected_stock, render_mode in rows:
        seen_modes.add(render_mode)
        url = f"{str(client.base_url).rstrip('/')}/mock-store/{slug}"
        html = fetch_html(url, client=client, policy=policy, sleep=lambda _: None)

        if render_mode == "jsonld":
            result = parse_product(html, parse_mode="auto")
            assert result.source == "jsonld"
            assert result.price == expected_price
            assert result.currency == expected_currency
            assert result.in_stock is expected_stock
        elif render_mode == "css_only":
            result = parse_product(html, parse_mode="auto")
            assert result.source == "css"
            assert result.price == expected_price
            assert result.currency == expected_currency
            assert result.in_stock is expected_stock
        elif render_mode == "broken":
            with pytest.raises(ParseError):
                parse_product(html, parse_mode="auto")
        else:
            pytest.fail(f"Unexpected render_mode: {render_mode}")

    assert seen_modes == {"jsonld", "css_only", "broken"}
