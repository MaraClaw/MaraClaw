import socket
import ssl
import threading
from collections.abc import Iterable
from typing import Never, Unpack, override

import anyio
import httpcore
import httpx
import pytest
from httpcore._backends.base import SOCKET_OPTION

from app.core import outbound_policy, outbound_transport
from app.core.outbound_policy import OutboundPolicyError
from app.core.outbound_transport import operator_http_client, public_http_client
from builtin_keyword_fakes import SocketTypeKeyword


class RecordingStream(httpcore.AsyncNetworkStream):
    def __init__(self, response: bytes) -> None:
        self.response: bytes = response
        self.written: bytes = b""
        self.tls: tuple[str | None, ssl.VerifyMode, bool] | None = None
        self.closed: bool = False

    @override
    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    @override
    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer

    @override
    async def aclose(self) -> None:
        self.closed = True

    @override
    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.tls = (server_hostname, ssl_context.verify_mode, ssl_context.check_hostname)
        return self


class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connections: list[str] = []
        self.streams: list[RecordingStream] = []
        self.responses: list[bytes] = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]
        self.fail_first: bool = False

    @override
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connections.append(host)
        if self.fail_first and len(self.connections) == 1:
            raise httpcore.ConnectError("first address failed")
        stream = RecordingStream(self.responses.pop(0))
        self.streams.append(stream)
        return stream


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> RecordingBackend:
    backend = RecordingBackend()
    monkeypatch.setattr(outbound_transport, "AnyIOBackend", lambda: backend)

    def lookup(
        host: str, port: int, **kwargs: Unpack[SocketTypeKeyword]
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, kwargs["type"], 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    return backend


async def test_real_http_pipeline_pins_ip_preserves_tls_and_host_ignores_proxies(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9999")
    async with public_http_client(timeout=1) as client:
        response = await client.get("https://public.example/status")
    assert response.text == "ok"
    assert wire.connections == ["8.8.8.8"]
    assert wire.streams[0].tls == ("public.example", ssl.CERT_REQUIRED, True)
    assert b"Host: public.example\r\n" in wire.streams[0].written
    assert wire.streams[0].closed


@pytest.mark.parametrize("location", ["http://127.0.0.1/secret", "https://other.example/secret"])
async def test_origin_guard_blocks_redirect_before_credentials_leave(wire: RecordingBackend, location: str) -> None:
    wire.responses = [f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()]
    async with public_http_client(timeout=1, follow_redirects=True, origin="https://public.example") as client:
        with pytest.raises(OutboundPolicyError):
            await client.get("https://public.example/start", headers={"Authorization": "Bearer secret"})
    assert wire.connections == ["8.8.8.8"]
    assert wire.streams[0].closed


async def test_public_redirect_to_private_address_is_blocked_without_origin(wire: RecordingBackend) -> None:
    wire.responses = [b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/\r\nContent-Length: 0\r\n\r\n"]
    async with public_http_client(timeout=1, follow_redirects=True) as client:
        with pytest.raises(OutboundPolicyError):
            await client.get("https://public.example/")
    assert wire.connections == ["8.8.8.8"]


async def test_same_origin_sse_stream_and_advertised_endpoint_guard(wire: RecordingBackend) -> None:
    body = b"event: endpoint\ndata: /messages\n\n"
    wire.responses = [
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    ]
    async with public_http_client(timeout=1, origin="https://public.example/mcp") as client:
        async with client.stream("GET", "https://public.example/sse") as response:
            assert await response.aread() == body
        with pytest.raises(OutboundPolicyError):
            await client.post("https://other.example/messages", headers={"Authorization": "secret"})
    assert wire.connections == ["8.8.8.8"]


@pytest.mark.parametrize("rebind", [False, True])
async def test_fallback_resolves_anew_and_rejects_rebinding(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch, rebind: bool
) -> None:
    calls = 0

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls > 1 and rebind:
            raise OutboundPolicyError("unsafe rebound answer")
        return ("8.8.8.8", "1.1.1.1") if calls == 1 else ("9.9.9.9", "208.67.222.222")

    wire.fail_first = True
    monkeypatch.setattr(outbound_transport, "resolve_public_addresses", resolve)
    async with public_http_client(timeout=1) as client:
        if rebind:
            with pytest.raises(OutboundPolicyError):
                await client.get("https://public.example/")
        else:
            assert (await client.get("https://public.example/")).text == "ok"
    assert calls == 2
    assert wire.connections == (["8.8.8.8"] if rebind else ["8.8.8.8", "208.67.222.222"])


async def test_dns_rebinding_after_standalone_validation_is_blocked(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = await outbound_policy.validate_public_url("https://public.example/")

    def lookup(
        host: str, port: int, **kwargs: Unpack[SocketTypeKeyword]
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, kwargs["type"], 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    async with public_http_client(timeout=1) as client:
        with pytest.raises(OutboundPolicyError):
            await client.get(url)
    assert wire.connections == []


async def test_dns_budget_maps_timeout_without_connecting(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(host: str, port: int) -> Never:
        await anyio.sleep_forever()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(outbound_transport, "resolve_public_addresses", resolve)
    with anyio.fail_after(1):
        async with public_http_client(timeout=0.01) as client:
            with pytest.raises(httpx.ConnectTimeout):
                await client.get("https://public.example/")
    assert wire.connections == []


async def test_cancellation_propagates_without_retry(wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = anyio.Event()
    cancelled = anyio.Event()

    async def resolve(host: str, port: int) -> Never:
        entered.set()
        await anyio.sleep_forever()
        raise AssertionError("Unreachable")

    async def request() -> None:
        async with public_http_client(timeout=1) as client:
            try:
                await client.get("https://public.example/")
            except anyio.get_cancelled_exc_class():
                cancelled.set()
                raise

    monkeypatch.setattr(outbound_transport, "resolve_public_addresses", resolve)
    async with anyio.create_task_group() as group:
        group.start_soon(request)
        await entered.wait()
        group.cancel_scope.cancel()
    assert cancelled.is_set()
    assert wire.connections == []


@pytest.mark.parametrize("override", ["host", "sni"])
async def test_host_and_sni_overrides_are_blocked(wire: RecordingBackend, override: str) -> None:
    async with public_http_client(timeout=1) as client:
        request = client.build_request("GET", "https://public.example/")
        if override == "host":
            request.headers["host"] = "internal.example"
        else:
            request.extensions["sni_hostname"] = "internal.example"
        with pytest.raises(OutboundPolicyError):
            await client.send(request)
    assert wire.connections == []


async def test_blocking_system_dns_obeys_connect_budget(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = threading.Event()

    def lookup(
        host: str, port: int, **kwargs: Unpack[SocketTypeKeyword]
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        released.wait()
        return [(socket.AF_INET, kwargs["type"], 6, "", ("8.8.8.8", port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    try:
        with anyio.fail_after(1):
            async with public_http_client(timeout=0.01) as client:
                with pytest.raises(httpx.ConnectTimeout):
                    await client.get("https://public.example/")
    finally:
        released.set()
    assert wire.connections == []


async def test_retry_dns_shares_original_budget(wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls > 1:
            await anyio.sleep_forever()
        return ("8.8.8.8", "1.1.1.1")

    wire.fail_first = True
    monkeypatch.setattr(outbound_transport, "resolve_public_addresses", resolve)
    with anyio.fail_after(1):
        async with public_http_client(timeout=0.01) as client:
            with pytest.raises(httpx.ConnectTimeout):
                await client.get("https://public.example/")
    assert calls == 2
    assert wire.connections == ["8.8.8.8"]


async def test_same_origin_redirect_rechecks_new_connection(wire: RecordingBackend) -> None:
    wire.responses.insert(0, b"HTTP/1.1 302 Found\r\nLocation: /next\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
    async with public_http_client(timeout=1, follow_redirects=True, origin="https://public.example") as client:
        response = await client.get("https://public.example/start")
    assert response.text == "ok"
    assert wire.connections == ["8.8.8.8", "8.8.8.8"]
    assert b"GET /next HTTP/1.1\r\n" in wire.streams[1].written
    assert all(stream.closed for stream in wire.streams)


async def test_policy_error_is_not_retried(wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def resolve(host: str, port: int) -> Never:
        nonlocal calls
        calls += 1
        raise OutboundPolicyError("unsafe answer")

    monkeypatch.setattr(outbound_transport, "resolve_public_addresses", resolve)
    async with public_http_client(timeout=1) as client:
        with pytest.raises(OutboundPolicyError):
            await client.get("https://public.example/")
    assert calls == 1
    assert wire.connections == []


async def test_operator_client_reaches_private_host_directly_with_verified_tls_and_no_proxies(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9999")
    async with operator_http_client(timeout=1, origin="https://10.0.0.5:8443/api") as client:
        response = await client.get("https://10.0.0.5:8443/status")
    assert response.text == "ok"
    assert wire.connections == ["10.0.0.5"]
    assert wire.streams[0].tls == ("10.0.0.5", ssl.CERT_REQUIRED, True)
    assert wire.streams[0].closed


@pytest.mark.parametrize("target", ["https://10.0.0.6:8443/x", "https://10.0.0.5:9443/x", "http://10.0.0.5:8443/x"])
async def test_operator_client_blocks_other_origins_before_connecting(wire: RecordingBackend, target: str) -> None:
    async with operator_http_client(timeout=1, origin="https://10.0.0.5:8443") as client:
        with pytest.raises(OutboundPolicyError):
            await client.get(target, headers={"Authorization": "Bearer secret"})
    assert wire.connections == []


@pytest.mark.parametrize("location", ["https://10.0.0.5:8443/next", "https://other.example/x"])
async def test_operator_client_never_follows_redirects(wire: RecordingBackend, location: str) -> None:
    wire.responses = [f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n".encode()]
    async with operator_http_client(timeout=1, origin="https://10.0.0.5:8443") as client:
        response = await client.get("https://10.0.0.5:8443/start")
    assert response.status_code == 302
    assert wire.connections == ["10.0.0.5"]


async def test_operator_client_blocks_host_and_sni_overrides(wire: RecordingBackend) -> None:
    async with operator_http_client(timeout=1, origin="https://10.0.0.5:8443") as client:
        request = client.build_request("GET", "https://10.0.0.5:8443/")
        request.headers["host"] = "internal.example"
        with pytest.raises(OutboundPolicyError):
            await client.send(request)
    assert wire.connections == []


async def test_public_client_still_rejects_private_hosts_after_operator_client_exists(
    wire: RecordingBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lookup(
        host: str, port: int, **kwargs: Unpack[SocketTypeKeyword]
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, kwargs["type"], 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    async with public_http_client(timeout=1, origin="https://10.0.0.5:8443") as client:
        with pytest.raises(OutboundPolicyError):
            await client.get("https://10.0.0.5:8443/")
    assert wire.connections == []
