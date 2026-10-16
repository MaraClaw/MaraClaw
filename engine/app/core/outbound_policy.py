"""Destination policy for untrusted outbound HTTP connections."""

import ipaddress
import socket
from functools import partial
from typing import Final

import anyio
import httpx

_TRANSITION_NETWORKS: Final = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)


class OutboundPolicyError(Exception):
    """An outbound destination violates the public-network policy."""


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise OutboundPolicyError("Invalid destination IP address") from None
    unsafe = not address.is_global or address.is_multicast or address.is_reserved or "%" in value
    if isinstance(address, ipaddress.IPv6Address):
        unsafe = unsafe or address.is_site_local or address.ipv4_mapped is not None
        unsafe = unsafe or any(address in network for network in _TRANSITION_NETWORKS)
    if unsafe:
        raise OutboundPolicyError(f"Private, local, reserved, or internal network URLs are blocked ({address})")
    return str(address)


def _parse_url(url: str) -> httpx.URL:
    """Parse without HTTPX's permissive repair of ambiguous input."""
    if not url.lower().startswith(("http://", "https://")):
        raise OutboundPolicyError("Only HTTP and HTTPS URLs are supported")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise OutboundPolicyError("Invalid outbound URL")
    authority = url.split("://", 1)[1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "%" in authority or "@" in authority:
        raise OutboundPolicyError("Encoded hosts, scoped addresses, and URL credentials are blocked")
    try:
        parsed = httpx.URL(url)
        if not parsed.host or parsed.port == 0:
            raise OutboundPolicyError("Invalid outbound hostname or port")
        return parsed.copy_with(host=parsed.host.rstrip("."))
    except httpx.InvalidURL, ValueError:
        raise OutboundPolicyError("Invalid outbound URL") from None


async def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve every answer, rejecting the entire result if any address is unsafe."""
    hostname = host.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise OutboundPolicyError("Localhost URLs are blocked")
    if not hostname or any(char in hostname for char in "%\\/@?#") or any(ord(char) <= 32 for char in hostname):
        raise OutboundPolicyError("Invalid outbound hostname")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return (_public_address(str(literal)),)
    try:
        answers = await anyio.to_thread.run_sync(
            partial(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM), abandon_on_cancel=True
        )
    except socket.gaierror as exc:
        raise OutboundPolicyError(f"Could not resolve hostname {hostname}: {exc.strerror}") from None
    addresses = tuple(dict.fromkeys(_public_address(str(answer[4][0])) for answer in answers))
    if not addresses:
        raise OutboundPolicyError(f"Could not resolve hostname {hostname}: no addresses")
    return addresses


async def validate_public_url(url: str) -> str:
    """Return a normalized HTTP URL after validating all destination addresses."""
    parsed = _parse_url(url)
    await resolve_public_addresses(parsed.host, parsed.port or (443 if parsed.scheme == "https" else 80))
    return str(parsed)


def ensure_same_origin(configured_url: str, target_url: str) -> None:
    """Reject credential destinations outside the configured HTTP origin."""
    configured, target = _parse_url(configured_url), _parse_url(target_url)
    if (configured.scheme, configured.raw_host, configured.port) != (target.scheme, target.raw_host, target.port):
        raise OutboundPolicyError("Cross-origin outbound requests are blocked")
