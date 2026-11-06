"""Process-wide HTTP client for outbound channel delivery.

One client keeps TLS connections to the provider hosts warm across chunks. It is
bound to the event loop that created it; a different running loop (tests, a new
lifespan) gets a fresh client instead of reusing sockets from a closed loop.
"""

import asyncio
from typing import override

import httpx

_TIMEOUT_SECONDS = 20
_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)


class _NoStoreCookies(httpx.Cookies):
    """Do not persist Set-Cookie across tenants that share the pooled client."""

    @override
    def extract_cookies(self, response: object) -> None:
        return None

    @override
    def set_cookie_header(self, request: object) -> None:
        return None


_client: httpx.AsyncClient | None = None
_loop: asyncio.AbstractEventLoop | None = None


def outbound_client() -> httpx.AsyncClient:
    """Return the shared client, creating it lazily for the running loop."""
    global _client, _loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _loop is not loop:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS, follow_redirects=False, limits=_LIMITS, cookies=_NoStoreCookies()
        )
        _loop = loop
    return _client


def reset_outbound_client() -> None:
    """Drop the shared client without closing it. Tests call this; open sockets are left to GC."""
    global _client, _loop
    _client = None
    _loop = None


async def shutdown_outbound_client() -> None:
    """Close the shared client. App lifespan calls this."""
    client, loop = _client, _loop
    reset_outbound_client()
    if client is not None and not client.is_closed and loop is asyncio.get_running_loop():
        await client.aclose()
