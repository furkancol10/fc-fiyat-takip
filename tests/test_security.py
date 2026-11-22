from __future__ import annotations

import socket

import pytest

from app.security import UrlNotAllowed, demo_policy, validate_url


def test_allowed_host_and_prefix_accepted_and_normalized() -> None:
    # Kabul: given a URL whose host is in allowed_hosts (case-insensitive) and path starts with
    # allowed_path_prefix, expect validate_url returns a normalized URL string
    url = validate_url(
        "HTTP://127.0.0.1:8000/mock-store/kahve-diyari#frag",
        allowed_hosts={"127.0.0.1"},
        allowed_path_prefix="/mock-store/",
    )
    assert url == "http://127.0.0.1:8000/mock-store/kahve-diyari"

    url_ci = validate_url(
        "https://DEMO.LOCAL/mock-store/item?a=1",
        allowed_hosts={"demo.local"},
        allowed_path_prefix="/mock-store/",
    )
    assert url_ci == "https://demo.local/mock-store/item?a=1"


def test_demo_policy_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Kabul: given MOCK_BASE_URL unset or overridden, expect demo_policy returns
    # allowed_hosts={base host} and allowed_path_prefix="/mock-store/"
    monkeypatch.delenv("MOCK_BASE_URL", raising=False)
    policy_default = demo_policy()
    assert policy_default.allowed_hosts == {"127.0.0.1"}
    assert policy_default.allowed_path_prefix == "/mock-store/"

    monkeypatch.setenv("MOCK_BASE_URL", "https://Shop.Example.COM:8443")
    policy_env = demo_policy()
    assert policy_env.allowed_hosts == {"shop.example.com"}
    assert policy_env.allowed_path_prefix == "/mock-store/"

    hosts, prefix = demo_policy("http://testserver")
    assert hosts == {"testserver"}
    assert prefix == "/mock-store/"


@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://127.0.0.1/item",
        "http://127.0.1.1:8080/item",
        "http://10.0.0.1/item",
        "http://172.16.0.1/item",
        "http://172.31.255.254/item",
        "http://192.168.1.1/item",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/item",
        "http://224.0.0.1/item",
        "http://[::1]/item",
        "http://[::]/item",
        "http://[fc00::1]/item",
        "http://[fd12:3456::1]/item",
        "http://[fe80::1]/item",
        "http://[ff02::1]/item",
    ],
)
def test_rejects_loopback_private_link_local_multicast_and_ipv6_non_public(
    blocked_url: str,
) -> None:
    # Kabul: given loopback, RFC1918 private, link-local/metadata, unspecified, multicast,
    # or non-public IPv6 literals not in allowed_hosts, expect UrlNotAllowed
    with pytest.raises(UrlNotAllowed):
        validate_url(blocked_url, allowed_hosts=set())


@pytest.mark.parametrize(
    "obfuscated_ip_url",
    [
        "http://2130706433/item",  # decimal 127.0.0.1
        "http://2852039166/latest/meta-data/",  # decimal 169.254.169.254
        "http://0x7f000001/item",  # hex 127.0.0.1
        "http://0x7f.0.0.1/item",  # dotted hex 127.0.0.1
        "http://0177.0.0.1/item",  # octal 127.0.0.1
        "http://017700000001/item",  # single octal 127.0.0.1
        "http://127.1/item",  # shorthand 127.0.0.1
    ],
)
def test_rejects_decimal_hex_octal_and_shorthand_private_ips(
    obfuscated_ip_url: str,
) -> None:
    # Kabul: given decimal, hex, octal, or shorthand IPv4 encodings of private/loopback IPs,
    # expect validate_url detects and rejects them with UrlNotAllowed
    with pytest.raises(UrlNotAllowed):
        validate_url(obfuscated_ip_url, allowed_hosts=set())


@pytest.mark.parametrize(
    "mapped_ipv6_url",
    [
        "http://[::ffff:127.0.0.1]/item",
        "http://[::ffff:7f00:1]/item",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[::ffff:10.0.0.1]/item",
        "http://[::ffff:8.8.8.8]/item",
    ],
)
def test_rejects_ipv4_mapped_ipv6(mapped_ipv6_url: str) -> None:
    # Kabul: given IPv4-mapped IPv6 literals, expect validate_url raises UrlNotAllowed
    with pytest.raises(UrlNotAllowed):
        validate_url(mapped_ipv6_url, allowed_hosts=set())


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://127.0.0.1/mock-store/item",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "gopher://127.0.0.1:6379/",
        "http://user:pass@127.0.0.1:8000/mock-store/item",
        "http://user@127.0.0.1:8000/mock-store/item",
        "http:///mock-store/item",
        "",
        "not-a-url",
    ],
)
def test_rejects_disallowed_schemes_userinfo_and_missing_host(bad_url: str) -> None:
    # Kabul: given non-http(s) scheme, userinfo credentials, or missing host,
    # expect validate_url raises UrlNotAllowed even if host matches allowed_hosts
    with pytest.raises(UrlNotAllowed):
        validate_url(
            bad_url,
            allowed_hosts={"127.0.0.1"},
            allowed_path_prefix="/mock-store/",
        )


@pytest.mark.parametrize(
    "traversal_url",
    [
        "http://127.0.0.1:8000/healthz",
        "http://127.0.0.1:8000/mock-store/../healthz",
        "http://127.0.0.1:8000/mock-store/foo/../../healthz",
        "http://127.0.0.1:8000/mock-store/%2e%2e/healthz",
        "http://127.0.0.1:8000/mock-store/%2E%2E/healthz",
        "http://127.0.0.1:8000/mock-store/.%2e/healthz",
        "http://127.0.0.1:8000/mock-store/%252e%252e/healthz",
        "http://127.0.0.1:8000/mock-store/..",
    ],
)
def test_rejects_prefix_mismatch_and_path_traversal(traversal_url: str) -> None:
    # Kabul: given a path outside allowed_path_prefix or containing .. / %2e%2e traversal,
    # expect validate_url raises UrlNotAllowed
    with pytest.raises(UrlNotAllowed):
        validate_url(
            traversal_url,
            allowed_hosts={"127.0.0.1"},
            allowed_path_prefix="/mock-store/",
        )


def test_injected_dns_resolver_rejects_private_and_mixed_ips_and_allows_public() -> None:
    # Kabul: given a hostname not in allowed_hosts and an injected DNS resolver,
    # expect UrlNotAllowed when any resolved IP is private/loopback, and acceptance when all are public
    def private_resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.42", port))]

    def mixed_resolver(host: str, port: int):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", port)),
        ]

    def public_resolver(host: str, port: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    def failing_resolver(host: str, port: int):
        raise socket.gaierror("NXDOMAIN")

    with pytest.raises(UrlNotAllowed):
        validate_url(
            "https://public.example/product",
            allowed_hosts=set(),
            resolver=private_resolver,
        )

    with pytest.raises(UrlNotAllowed):
        validate_url(
            "https://public.example/product",
            allowed_hosts=set(),
            resolver=mixed_resolver,
        )

    with pytest.raises(UrlNotAllowed):
        validate_url(
            "https://nonexistent.example/product",
            allowed_hosts=set(),
            resolver=failing_resolver,
        )

    normalized = validate_url(
        "https://public.example/product",
        allowed_hosts=set(),
        resolver=public_resolver,
    )
    assert normalized == "https://public.example/product"


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.com:8000/mock-store/x",
        "http://127.0.0.1.evil.com:8000/mock-store/x",
        "http://127.0.0.1:9999/mock-store/x",
        "http://127.0.0.1/mock-store/x",
        "http://8.8.8.8:8000/mock-store/x",
    ],
)
def test_demo_policy_is_strict_about_host_and_port(url: str) -> None:
    # Kabul: given demo policy (base http://127.0.0.1:8000), expect any other host/port rejected
    # even when the host resolves to a public IP
    from app.fetcher import _unpack_policy  # noqa: F401

    policy = demo_policy("http://127.0.0.1:8000")
    public = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]  # noqa: E731
    with pytest.raises(UrlNotAllowed):
        validate_url(
            url,
            allowed_hosts=policy.allowed_hosts,
            allowed_path_prefix=policy.allowed_path_prefix,
            strict_hosts=getattr(policy, "strict", False),
            strict_port=getattr(policy, "port", None),
            resolver=public,
        )


def test_demo_policy_strict_accepts_exact_host_port_and_trailing_dot() -> None:
    # Kabul: given exact allowed host+port (case, one trailing dot normalized), expect accepted
    policy = demo_policy("http://Shop.Local:8000")
    assert policy.strict is True and policy.port == 8000
    kw = dict(
        allowed_hosts=policy.allowed_hosts,
        allowed_path_prefix=policy.allowed_path_prefix,
        strict_hosts=policy.strict,
        strict_port=policy.port,
        resolver=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DNS")),
    )
    assert validate_url("http://SHOP.local.:8000/mock-store/a", **kw).endswith("/mock-store/a")
    default_port = demo_policy("https://shop.local")
    assert default_port.port == 443


def test_fetch_html_rejects_foreign_host_under_demo_policy() -> None:
    # Kabul: fetch_html with demo policy must reject evil.com before any network use
    from app.fetcher import fetch_html

    policy = demo_policy("http://127.0.0.1:8000")
    with pytest.raises(UrlNotAllowed):
        fetch_html(
            "http://evil.com:8000/mock-store/x",
            policy=policy,
            resolver=lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
