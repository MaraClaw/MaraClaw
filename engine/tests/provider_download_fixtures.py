"""Controlled DNS and dial boundary for the actual public HTTP transport."""

import socket
from collections.abc import Callable
from typing import Unpack

import httpx
import pytest

from app.core import outbound_transport
from builtin_keyword_fakes import SocketTypeKeyword
from channel_media_fixtures import REAL_CLIENT
from test_outbound_transport import RecordingBackend


def response_bytes(body: bytes = b"image-bytes", *, status: int = 200, headers: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status} Response\r\nContent-Length: {len(body)}\r\nConnection: close\r\n{headers}\r\n".encode()
        + body
    )


def dns_answers(monkeypatch: pytest.MonkeyPatch, answers: Callable[[str], tuple[str, ...]]) -> None:
    def lookup(
        host: str, port: int, **kwargs: Unpack[SocketTypeKeyword]
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, kwargs["type"], 6, "", (address, port)) for address in answers(host)]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)


@pytest.fixture
def download_wire(monkeypatch: pytest.MonkeyPatch) -> RecordingBackend:
    backend = RecordingBackend()
    backend.responses = [response_bytes()]
    monkeypatch.setattr(outbound_transport, "AnyIOBackend", lambda: backend)
    dns_answers(monkeypatch, lambda _: ("8.8.8.8",))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9999")
    return backend


def generation_api(monkeypatch: pytest.MonkeyPatch, image_url: str) -> list[httpx.Request]:
    """Fake only configured generation HTTP; preserve any real public download transport."""
    requests: list[httpx.Request] = []

    def generate(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST", "A result download reused the generation client"
        return httpx.Response(
            200,
            json={"data": [{"url": image_url}]},
            headers={"set-cookie": "generation-session=private; Path=/"},
        )

    ordinary_api(monkeypatch, generate)
    return requests


def ordinary_api(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    def client(
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
        follow_redirects: bool = False,
        trust_env: bool = True,
    ) -> httpx.AsyncClient:
        return REAL_CLIENT(
            timeout=timeout,
            transport=transport if transport is not None else httpx.MockTransport(handler),
            follow_redirects=follow_redirects,
            trust_env=trust_env,
        )

    monkeypatch.setattr(httpx, "AsyncClient", client)
