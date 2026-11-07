"""Process-wide HTTP client for the Tavily proxy, separate from IM delivery's ``outbound_client``.

One client keeps TLS connections to api.tavily.com warm across retries and key rotation. It is bound to
the event loop that created it; a different running loop (tests, a new lifespan) gets a fresh client instead of
reusing sockets from a closed loop. Redirects are followed by the caller, not here.
"""

from __future__ import annotations

import asyncio
from http.cookiejar import Cookie, CookieJar
from typing import override
from urllib.request import Request

import httpx

_DEFAULT_TIMEOUT_SECONDS = 60.0
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0)


class _NoStoreJar(CookieJar):
    """Never keep or replay a Set-Cookie: the client is shared by every agent.

    httpx rebuilds ``client.cookies`` as a plain ``Cookies`` around whatever is passed, so a ``Cookies`` subclass is
    discarded. It keeps a ``CookieJar`` instance as is, and its redirect requests copy from that same jar.
    """

    @override
    def set_cookie(self, cookie: Cookie) -> None:
        return None

    @override
    def add_cookie_header(self, request: Request) -> None:
        return None


_client: httpx.AsyncClient | None = None
_loop: asyncio.AbstractEventLoop | None = None


def tavily_client() -> httpx.AsyncClient:
    """Return the shared client, creating it lazily for the running loop. Never ``async with`` it."""
    global _client, _loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _loop is not loop:
        _client = httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT_SECONDS, follow_redirects=False, limits=_LIMITS, cookies=_NoStoreJar()
        )
        _loop = loop
    return _client


def reset_tavily_client() -> None:
    """Drop the shared client without closing it. Tests call this; open sockets are left to GC."""
    global _client, _loop
    _client = None
    _loop = None


async def shutdown_tavily_client() -> None:
    """Close the shared client. App lifespan calls this."""
    client, loop = _client, _loop
    reset_tavily_client()
    if client is not None and not client.is_closed and loop is asyncio.get_running_loop():
        await client.aclose()
