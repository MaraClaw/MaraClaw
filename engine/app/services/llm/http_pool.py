"""Process-wide httpx clients so LLM wrappers reuse TLS connections.

Callers still construct lightweight ``LLMClient`` wrappers and still call
``close()``. The wrapper detaches; it does not shut down the shared transport.
"""

from __future__ import annotations

import httpx

_HTTP: dict[float, httpx.AsyncClient] = {}


def acquire_httpx(timeout: float) -> httpx.AsyncClient:
    """Return a shared AsyncClient for this timeout. Safe to call from async code."""
    client = _HTTP.get(timeout)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=None)
        _HTTP[timeout] = client
    return client


def reset_http_pool() -> None:
    """Drop pooled transports. Tests call this; open sockets are left to GC."""
    _HTTP.clear()
