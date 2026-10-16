import socket
from collections.abc import Callable

import pytest

from app.core.outbound_policy import (
    OutboundPolicyError,
    ensure_same_origin,
    resolve_public_addresses,
    validate_public_url,
)


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], None]:
    def configure(addresses: list[str]) -> None:
        def lookup(host: str, port: int, *, type: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
            assert type == socket.SOCK_STREAM
            return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]

        monkeypatch.setattr(socket, "getaddrinfo", lookup)

    return configure


@pytest.mark.parametrize(
    "url",
    [
        "example.com",
        "ftp://example.com",
        "//example.com",
        "http:///missing",
        "http://user:secret@example.com",
        "http://%31%32%37.0.0.1",
        "http://example.com\\@127.0.0.1",
        "http://example.com:bad",
        "http://example.com:0",
        "http://[fe80::1%25en0]",
        "http://example.com\n/path",
    ],
)
async def test_rejects_ambiguous_urls_before_dns(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_lookup() -> None:
        pytest.fail("Invalid URLs must not resolve")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_lookup)
    with pytest.raises(OutboundPolicyError):
        await validate_public_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "198.18.0.1",
        "192.0.2.1",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",  # noqa: S104 - blocked destination test, not a bind
        "::",
        "::1",
        "fc00::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",
        "2002:7f00:1::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "64:ff9b::7f00:1",
        "64:ff9b:1::1",
    ],
)
async def test_rejects_unsafe_literal_and_dns_answers(address: str, dns: Callable[[list[str]], None]) -> None:
    dns(["8.8.8.8", address])
    with pytest.raises(OutboundPolicyError):
        await resolve_public_addresses("public.example", 443)
    with pytest.raises(OutboundPolicyError):
        await resolve_public_addresses(address, 443)


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST.", "service.localhost"])
async def test_blocks_localhost_names(host: str) -> None:
    with pytest.raises(OutboundPolicyError, match="Localhost URLs are blocked"):
        await resolve_public_addresses(host, 80)


async def test_normalizes_url_and_deduplicates_public_answers(dns: Callable[[list[str]], None]) -> None:
    dns(["8.8.8.8", "2606:4700:4700::1111", "8.8.8.8"])
    assert await validate_public_url("HTTPS://EXAMPLE.COM.:443/a?token=secret") == "https://example.com/a?token=secret"
    assert await resolve_public_addresses("example.com", 443) == ("8.8.8.8", "2606:4700:4700::1111")


@pytest.mark.parametrize("addresses", [[], ["8.8.8.8", "127.0.0.1"]])
async def test_fails_closed_on_empty_or_mixed_answers(addresses: list[str], dns: Callable[[list[str]], None]) -> None:
    dns(addresses)
    with pytest.raises(OutboundPolicyError):
        await validate_public_url("https://example.com/private/path?secret=hidden")


async def test_dns_error_does_not_include_path_or_query(monkeypatch: pytest.MonkeyPatch) -> None:
    def lookup(host: str, port: int, *, type: int) -> None:
        raise socket.gaierror(socket.EAI_NONAME, "not found")

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    with pytest.raises(OutboundPolicyError, match=r"Could not resolve hostname example\.com: not found") as caught:
        await validate_public_url("https://example.com/private?secret=hidden")
    assert "hidden" not in str(caught.value)
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("target", ["https://EXAMPLE.com:443/b", "https://example.com./events?x=1"])
def test_origin_allows_normalized_same_origin(target: str) -> None:
    ensure_same_origin("https://example.com/a", target)


@pytest.mark.parametrize("target", ["http://example.com", "https://other.example", "https://example.com:444"])
def test_origin_rejects_scheme_host_and_port_changes(target: str) -> None:
    with pytest.raises(OutboundPolicyError, match="Cross-origin"):
        ensure_same_origin("https://example.com", target)
