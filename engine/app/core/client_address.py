"""The address a request came from, honoring forwarded headers only from configured proxies.

Behind a reverse proxy the socket peer is the proxy, so ``request.client.host`` puts every user in one bucket. When the
peer is listed in ``TRUSTED_PROXIES`` the ``X-Forwarded-For`` chain is walked from the right, skipping trusted hops;
the first other address is the client. From any other peer the header is attacker-controlled and ignored.
"""

from functools import lru_cache
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

from fastapi import Request

from app.config import get_settings

type _Network = IPv4Network | IPv6Network


@lru_cache(maxsize=8)
def _networks(entries: tuple[str, ...]) -> tuple[_Network, ...]:
    return tuple(ip_network(entry, strict=False) for entry in entries)


def _parse(value: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(value.strip())
    except ValueError:
        return None


def _is_trusted(address: IPv4Address | IPv6Address | None, trusted: tuple[_Network, ...]) -> bool:
    return address is not None and any(address in network for network in trusted)


async def get_client_ip(request: Request) -> str | None:
    """Resolve the client address; ``None`` when the request carries no peer."""
    if request.client is None:
        return None
    peer = request.client.host
    trusted = _networks(tuple(get_settings().TRUSTED_PROXIES))
    if not _is_trusted(_parse(peer), trusted):
        return peer
    resolved = peer
    for hop in reversed(request.headers.get("x-forwarded-for", "").split(",")):
        address = _parse(hop)
        if address is None:
            break
        resolved = str(address)
        if not _is_trusted(address, trusted):
            break
    return resolved
