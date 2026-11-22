from __future__ import annotations

import ipaddress
import os
import posixpath
import re
import socket
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit, urlunsplit

DEFAULT_MOCK_BASE_URL = "http://127.0.0.1:8000"
ALLOWED_SCHEMES = frozenset({"http", "https"})

_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_HEX_PART_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")
_OCTAL_PART_RE = re.compile(r"^0[0-7]+$")
_DECIMAL_PART_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")


class UrlNotAllowed(Exception):
    """Raised when a URL violates SSRF or allowlist policy."""


class UrlPolicy(NamedTuple):
    allowed_hosts: set[str]
    allowed_path_prefix: str | None = None


def demo_policy(base_url: str | None = None) -> UrlPolicy:
    """Build the default demo allowlist policy for /mock-store/ endpoints."""
    effective_base = (
        base_url
        if base_url is not None
        else (os.environ.get("MOCK_BASE_URL") or DEFAULT_MOCK_BASE_URL)
    )
    parts = urlsplit(effective_base.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ALLOWED_SCHEMES or not host:
        raise UrlNotAllowed(f"Invalid demo base URL: {effective_base!r}")
    return UrlPolicy(allowed_hosts={host.lower()}, allowed_path_prefix="/mock-store/")


def _parse_ip_part(part: str) -> int | None:
    if _HEX_PART_RE.match(part):
        return int(part, 16)
    if _OCTAL_PART_RE.match(part):
        return int(part, 8)
    if _DECIMAL_PART_RE.match(part):
        return int(part, 10)
    return None


def _parse_nonstandard_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Parse decimal, hex, octal or shorthand IPv4 notations (WHATWG/inet_aton)."""
    candidate = host[:-1] if host.endswith(".") and len(host) > 1 else host
    parts = candidate.split(".")
    if not (1 <= len(parts) <= 4):
        return None

    nums: list[int] = []
    for part in parts:
        if not part:
            return None
        parsed = _parse_ip_part(part)
        if parsed is None:
            return None
        nums.append(parsed)

    if len(nums) == 1:
        if nums[0] > 0xFFFFFFFF:
            raise UrlNotAllowed(f"Invalid IPv4 literal: {host!r}")
        addr_int = nums[0]
    elif len(nums) == 2:
        if nums[0] > 0xFF or nums[1] > 0xFFFFFF:
            raise UrlNotAllowed(f"Invalid IPv4 literal: {host!r}")
        addr_int = (nums[0] << 24) | nums[1]
    elif len(nums) == 3:
        if nums[0] > 0xFF or nums[1] > 0xFF or nums[2] > 0xFFFF:
            raise UrlNotAllowed(f"Invalid IPv4 literal: {host!r}")
        addr_int = (nums[0] << 24) | (nums[1] << 16) | nums[2]
    else:
        if any(n > 0xFF for n in nums):
            raise UrlNotAllowed(f"Invalid IPv4 literal: {host!r}")
        addr_int = (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]

    return ipaddress.IPv4Address(addr_int)


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return _parse_nonstandard_ipv4(host)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return False
        if ip.sixtofour is not None or ip.teredo is not None:
            return False
        if ip in _NAT64_WELL_KNOWN:
            return False

    return bool(
        ip.is_global
        and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    )


def _decode_path_iteratively(path: str, max_rounds: int = 4) -> str:
    current = path
    for _ in range(max_rounds):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def _validate_path(raw_path: str, allowed_path_prefix: str | None) -> str:
    effective_raw = raw_path or "/"
    decoded = _decode_path_iteratively(effective_raw)

    if "\x00" in effective_raw or "\x00" in decoded:
        raise UrlNotAllowed("Null byte in URL path")
    if "\\" in effective_raw or "\\" in decoded:
        raise UrlNotAllowed("Backslash in URL path")

    if ".." in effective_raw.split("/") or ".." in decoded.split("/"):
        raise UrlNotAllowed("Path traversal ('..') is not allowed")

    if allowed_path_prefix is not None:
        if not effective_raw.startswith(allowed_path_prefix):
            raise UrlNotAllowed(
                f"Path {effective_raw!r} does not start with {allowed_path_prefix!r}"
            )
        if not decoded.startswith(allowed_path_prefix):
            raise UrlNotAllowed(
                f"Decoded path {decoded!r} does not start with {allowed_path_prefix!r}"
            )
        normalized = posixpath.normpath(decoded)
        if decoded.endswith("/") and not normalized.endswith("/"):
            normalized += "/"
        if not normalized.startswith(allowed_path_prefix):
            raise UrlNotAllowed(
                f"Normalized path {normalized!r} escapes {allowed_path_prefix!r}"
            )

    return effective_raw


def validate_url(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allowed_path_prefix: str | None = None,
    resolver: Callable[..., list[Any]] = socket.getaddrinfo,
) -> str:
    """Validate and normalize a URL against SSRF rules and optional host/path allowlists."""
    if not isinstance(url, str):
        raise UrlNotAllowed("URL must be a string")

    raw = url.strip()
    if not raw:
        raise UrlNotAllowed("URL is empty")

    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in raw):
        raise UrlNotAllowed("URL contains whitespace or control characters")
    if "\\" in raw:
        raise UrlNotAllowed("URL contains backslash")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlNotAllowed(f"Disallowed scheme: {scheme!r}")

    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UrlNotAllowed("Userinfo in URL is not allowed")

    host = parts.hostname
    if not host:
        raise UrlNotAllowed("URL host is required")
    if "%" in host:
        raise UrlNotAllowed("IPv6 zone identifier is not allowed")

    try:
        port = parts.port
    except ValueError as exc:
        raise UrlNotAllowed(f"Invalid port in URL: {url!r}") from exc
    if port is not None and port <= 0:
        raise UrlNotAllowed(f"Invalid port: {port}")

    safe_path = _validate_path(parts.path, allowed_path_prefix)

    normalized_allowed = {h.strip().lower().strip("[]") for h in allowed_hosts}
    host_lower = host.lower()

    if host_lower in normalized_allowed:
        canonical_host = host_lower
    else:
        ip_literal = _parse_ip_literal(host_lower)
        if ip_literal is not None:
            if not _is_public_ip(ip_literal):
                raise UrlNotAllowed(f"Non-public IP address is not allowed: {ip_literal}")
            canonical_host = str(ip_literal)
        else:
            effective_port = port or (443 if scheme == "https" else 80)
            try:
                addr_infos = resolver(host_lower, effective_port)
            except (socket.gaierror, OSError) as exc:
                raise UrlNotAllowed(f"Host resolution failed for {host_lower!r}") from exc

            if not addr_infos:
                raise UrlNotAllowed(f"No IP addresses resolved for {host_lower!r}")

            for info in addr_infos:
                ip_str = info[4][0]
                if "%" in ip_str:
                    raise UrlNotAllowed(f"Scoped IP address is not allowed: {ip_str!r}")
                try:
                    resolved_ip = ipaddress.ip_address(ip_str)
                except ValueError as exc:
                    raise UrlNotAllowed(f"Invalid resolved IP: {ip_str!r}") from exc
                if not _is_public_ip(resolved_ip):
                    raise UrlNotAllowed(
                        f"Host {host_lower!r} resolves to non-public IP {resolved_ip}"
                    )
            canonical_host = host_lower

    host_formatted = f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    default_port = 443 if scheme == "https" else 80
    netloc = (
        f"{host_formatted}:{port}"
        if port is not None and port != default_port
        else host_formatted
    )
    return urlunsplit((scheme, netloc, safe_path, parts.query, ""))
