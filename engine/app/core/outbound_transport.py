"""HTTPX transport that pins each connection to validated public DNS answers."""

import ssl
from collections.abc import Iterable
from typing import override

import anyio
import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend
from httpcore._backends.base import SOCKET_OPTION

from app.core.outbound_policy import OutboundPolicyError, _parse_url, ensure_same_origin, resolve_public_addresses


class _PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, timeout: float) -> None:
        self._timeout: float = timeout
        self._inner: httpcore.AsyncNetworkBackend = AnyIOBackend()

    @override
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        budget = min(timeout, self._timeout) if timeout is not None else self._timeout
        try:
            with anyio.fail_after(budget) as scope:
                addresses = await resolve_public_addresses(host, port)
                attempts = len(addresses)
                for attempt in range(attempts):
                    if attempt:
                        addresses = await resolve_public_addresses(host, port)
                    try:
                        return await self._inner.connect_tcp(
                            addresses[attempt % len(addresses)],
                            port,
                            timeout=max(0.0, scope.deadline - anyio.current_time()),
                            local_address=local_address,
                            socket_options=socket_options,
                        )
                    except httpcore.ConnectError, httpcore.ConnectTimeout:
                        if attempt + 1 == attempts:
                            raise
        except TimeoutError:
            raise httpcore.ConnectTimeout("Public destination connection timed out") from None
        raise httpcore.ConnectError("No public destination addresses")


class _PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, timeout: float, origin: str | None, *, public_only: bool = True) -> None:
        context = ssl.create_default_context()
        super().__init__(verify=context, trust_env=False)
        self._origin: str | None = str(_parse_url(origin)) if origin is not None else None
        self._pool: httpcore.AsyncConnectionPool = httpcore.AsyncConnectionPool(
            ssl_context=context,
            network_backend=_PublicNetworkBackend(timeout) if public_only else AnyIOBackend(),
            retries=0,
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
        )

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        parsed = _parse_url(str(request.url))
        if self._origin is not None:
            ensure_same_origin(self._origin, str(parsed))
        if "sni_hostname" in request.extensions:
            raise OutboundPolicyError("Overridden TLS hostnames are blocked")
        expected_host = httpx.Request("GET", request.url).headers["host"]
        if request.headers.get("host") != expected_host:
            raise OutboundPolicyError("Overridden HTTP Host headers are blocked")
        return await super().handle_async_request(request)


def public_http_client(
    *, timeout: float, follow_redirects: bool = False, origin: str | None = None
) -> httpx.AsyncClient:
    """Create an owned, verified, proxy-free public-destination HTTP client."""
    return httpx.AsyncClient(
        transport=_PublicHTTPTransport(timeout, origin),
        timeout=timeout,
        follow_redirects=follow_redirects,
        trust_env=False,
    )


def operator_http_client(*, timeout: float, origin: str) -> httpx.AsyncClient:
    """Create a client for an operator-owned endpoint that may resolve to a private address.

    Only server-established configuration may call this. The origin guard, TLS verification, proxy-free
    transport and absence of redirects match ``public_http_client``; only the public-address check is lifted.
    """
    return httpx.AsyncClient(
        transport=_PublicHTTPTransport(timeout, origin, public_only=False),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )
