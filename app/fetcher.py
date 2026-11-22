from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from app.security import UrlPolicy, validate_url

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MB
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_RETRIES = 2
BASE_BACKOFF_SECONDS = 0.5
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


class FetchError(Exception):
    """Raised when an HTTP fetch fails due to transport, status, size, or content type."""


def _unpack_policy(
    policy: UrlPolicy | tuple[Iterable[str], str | None],
) -> tuple[Iterable[str], str | None]:
    if isinstance(policy, UrlPolicy):
        return policy.allowed_hosts, policy.allowed_path_prefix
    if hasattr(policy, "allowed_hosts") and hasattr(policy, "allowed_path_prefix"):
        return policy.allowed_hosts, policy.allowed_path_prefix
    allowed_hosts, allowed_path_prefix = policy
    return allowed_hosts, allowed_path_prefix


def _read_bounded_html(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        raise FetchError(f"Unsupported content-type: {content_type!r}")

    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = -1
        if declared_size > MAX_BODY_BYTES:
            raise FetchError(
                f"Response Content-Length {declared_size} exceeds {MAX_BODY_BYTES} bytes"
            )

    buffer = bytearray()
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        if len(buffer) + len(chunk) > MAX_BODY_BYTES:
            raise FetchError(f"Response body exceeds {MAX_BODY_BYTES} bytes")
        buffer.extend(chunk)

    encoding = response.encoding or "utf-8"
    return bytes(buffer).decode(encoding, errors="replace")


def fetch_html(
    url: str,
    *,
    client: httpx.Client | None = None,
    policy: UrlPolicy | tuple[Iterable[str], str | None],
    sleep: Callable[[float], None] = time.sleep,
    resolver: Callable[..., list[Any]] = socket.getaddrinfo,
) -> str:
    """Validate URL via policy and fetch bounded HTML with retries on transient errors."""
    allowed_hosts, allowed_path_prefix = _unpack_policy(policy)
    safe_url = validate_url(
        url,
        allowed_hosts=allowed_hosts,
        allowed_path_prefix=allowed_path_prefix,
        resolver=resolver,
        strict_hosts=bool(getattr(policy, "strict", False)),
        strict_port=getattr(policy, "port", None),
    )

    owns_client = client is None
    active_client = (
        httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=False)
        if owns_client
        else client
    )

    try:
        for attempt in range(MAX_RETRIES + 1):
            try:
                with active_client.stream(
                    "GET",
                    safe_url,
                    follow_redirects=False,
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                ) as response:
                    status = response.status_code

                    if 300 <= status < 400:
                        raise FetchError(f"Redirect not allowed: HTTP {status}")

                    if 400 <= status < 500:
                        raise FetchError(f"HTTP client error: {status}")

                    if 500 <= status < 600:
                        if attempt < MAX_RETRIES:
                            sleep(BASE_BACKOFF_SECONDS * (2**attempt))
                            continue
                        raise FetchError(f"HTTP server error after retries: {status}")

                    if not (200 <= status < 300):
                        raise FetchError(f"Unexpected HTTP status: {status}")

                    return _read_bounded_html(response)

            except FetchError:
                raise
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                if attempt < MAX_RETRIES:
                    sleep(BASE_BACKOFF_SECONDS * (2**attempt))
                    continue
                raise FetchError(f"Network error after retries: {exc}") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"HTTP transport error: {exc}") from exc

        raise FetchError("Exhausted fetch retries")
    finally:
        if owns_client:
            active_client.close()
