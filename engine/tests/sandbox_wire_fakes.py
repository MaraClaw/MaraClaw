import http
import json
import socket
import ssl
from collections.abc import Callable, Iterable
from typing import override

import httpcore
import pytest
from httpcore._backends.base import SOCKET_OPTION

from app.core import outbound_transport
from app.core.json_types import json_loads_value
from app.services.sandbox.api.judge0_backend import Judge0Backend
from app.services.sandbox.base import ExecutionResult, SandboxBackend
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend
from app.services.sandbox.remote.self_hosted_backend import SelfHostedBackend

PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
KEY = "sk-adapter-secret"


class Stream(httpcore.AsyncNetworkStream):
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
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.AsyncNetworkStream:
        self.tls = (server_hostname, ssl_context.verify_mode, ssl_context.check_hostname)
        return self

    @property
    def request_line(self) -> str:
        return self.written.split(b"\r\n", 1)[0].decode()

    @property
    def body(self) -> object:
        return json_loads_value(self.written.split(b"\r\n\r\n", 1)[1])


class Wire(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connections: list[str] = []
        self.streams: list[Stream] = []
        self.responses: list[bytes] = []
        self.answers: list[str] = ["8.8.8.8"]
        self.lookups: int = 0

    @override
    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if not self.responses:
            raise httpcore.ConnectError("unexpected connection")
        self.connections.append(host)
        self.streams.append(Stream(self.responses.pop(0)))
        return self.streams[-1]


def reply(status: int, body: object = None, *, location: str | None = None) -> bytes:
    payload = b"" if body is None else json.dumps(body).encode()
    head = f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}\r\nConnection: close\r\n"
    if location is not None:
        head += f"Location: {location}\r\n"
    return f"{head}Content-Length: {len(payload)}\r\n\r\n".encode() + payload


def install_wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    backend = Wire()
    monkeypatch.setattr(outbound_transport, "AnyIOBackend", lambda: backend)

    def lookup(host: str, port: int, *, type: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        answer = backend.answers[min(backend.lookups, len(backend.answers) - 1)]
        backend.lookups += 1
        return [(socket.AF_INET, type, 6, "", (answer, port))]

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    for name in PROXY_ENV:
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    return backend


type Factory = Callable[[SandboxConfig], SandboxBackend]
KINDS: dict[str, tuple[SandboxType, Factory, str]] = {
    "judge0": (SandboxType.JUDGE0, Judge0Backend, "https://judge.tenant.example"),
    "self_hosted": (SandboxType.SELF_HOSTED, SelfHostedBackend, "https://box.tenant.example/v1/shell/exec"),
    "aio_sandbox": (SandboxType.AIO_SANDBOX, AioSandboxBackend, "https://aio.tenant.example"),
}
EXECUTE_REPLIES: dict[str, list[bytes]] = {
    "judge0": [
        reply(201, {"token": "tok"}),
        reply(200, {}),
        reply(200, {"status": {"id": 3}, "stdout": "out", "stderr": "", "exit_code": 0}),
    ],
    "self_hosted": [reply(200, {"success": True, "stdout": "out"})],
    "aio_sandbox": [reply(200, {"success": True, "data": {"output": "out", "exit_code": 0}})],
}


def build(kind: str, url: str | None = None, *, operator: bool = False) -> SandboxBackend:
    sandbox_type, factory, default_url = KINDS[kind]
    config = SandboxConfig(type=sandbox_type, api_url=url or default_url, api_key=KEY)
    return factory(config.grant_operator_endpoint() if operator else config)


async def run(backend: SandboxBackend, operation: str) -> bool | ExecutionResult:
    if operation == "health":
        return await backend.health_check()
    return await backend.execute("print(1)", "bash", exec_timeout=5)


def queue(wire: Wire, kind: str, operation: str) -> int:
    wire.responses = [reply(200, [])] if operation == "health" else list(EXECUTE_REPLIES[kind])
    return len(wire.responses)


def succeeded(result: object) -> bool:
    return result is True or getattr(result, "success", False) is True
