import asyncio
import ssl
import sys
from types import SimpleNamespace

import httpcore
import pytest
from tests.sandbox_wire_fakes import (
    EXECUTE_REPLIES,
    KEY,
    KINDS,
    StalledReply,
    Wire,
    build,
    install_wire,
    queue,
    reply,
    run,
    succeeded,
)

from app.services.sandbox.api.judge0_backend import Judge0Backend
from app.services.sandbox.base import ExecutionDeadline, ExecutionResult, TerminationReason
from app.services.sandbox.config import ENDPOINT_BLOCKED_MESSAGE, SandboxConfig, SandboxType


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    return install_wire(monkeypatch)


@pytest.mark.parametrize("operation", ["health", "execute"])
@pytest.mark.parametrize("kind", KINDS)
async def test_public_endpoint_pins_validated_address_verifies_tls_and_ignores_proxy_env(
    wire: Wire, kind: str, operation: str
) -> None:
    # Given: a tenant-controlled public endpoint and proxy variables in the environment.
    backend, expected = build(kind), queue(wire, kind, operation)
    # When
    result = await run(backend, operation)
    # Then: every request dials the validated address, verifies TLS for the configured host and is closed.
    assert succeeded(result)
    assert wire.connections == ["8.8.8.8"] * expected
    assert {stream.tls for stream in wire.streams} == {(KINDS[kind][2].split("/")[2], ssl.CERT_REQUIRED, True)}
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"])
@pytest.mark.parametrize("operation", ["health", "execute"])
@pytest.mark.parametrize("kind", KINDS)
async def test_tenant_endpoint_resolving_private_is_rejected_with_fixed_error(
    wire: Wire, monkeypatch: pytest.MonkeyPatch, kind: str, operation: str, address: str
) -> None:
    # Given: a tenant hostname that resolves to a non-public address, and a logger that forbids tracebacks.
    wire.answers = [address]
    queue(wire, kind, operation)
    backend = build(kind)
    warnings: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("policy failures must not log a traceback")

    spy = SimpleNamespace(
        exception=forbidden, warning=lambda message, *a, **k: warnings.append(message), debug=forbidden, info=forbidden
    )
    monkeypatch.setattr(sys.modules[type(backend).__module__], "logger", spy)
    # When
    result = await run(backend, operation)
    # Then: nothing connects, the error is fixed and reveals neither the address nor the key.
    assert wire.connections == []
    if operation == "health":
        assert result is False
    else:
        assert isinstance(result, ExecutionResult)
        assert result.error == ENDPOINT_BLOCKED_MESSAGE
        assert (result.success, result.exit_code) == (False, 1)
        assert address not in repr(result)
        assert KEY not in repr(result)
    assert address not in "".join(warnings)


@pytest.mark.parametrize("kind", KINDS)
async def test_dns_change_after_a_healthy_probe_is_caught_at_connection_time(wire: Wire, kind: str) -> None:
    # Given: the hostname resolves publicly for the health probe and privately afterwards.
    wire.answers = ["8.8.8.8", "127.0.0.1"]
    wire.responses = [reply(200, [])]
    backend = build(kind)
    # When
    healthy = await backend.health_check()
    queue(wire, kind, "execute")
    result = await backend.execute("print(1)", "bash", exec_timeout=5)
    # Then: the execute dial re-validates DNS and is blocked without a second connection.
    assert healthy is True
    assert result.error == ENDPOINT_BLOCKED_MESSAGE
    assert wire.connections == ["8.8.8.8"]


async def test_judge0_polling_revalidates_dns_on_each_connection(wire: Wire) -> None:
    # Given: submission reaches a public address, then DNS rebinds to loopback before polling.
    wire.answers = ["8.8.8.8", "127.0.0.1"]
    wire.responses = list(EXECUTE_REPLIES["judge0"])
    # When
    result = await build("judge0").execute("print(1)", "python", exec_timeout=5)
    # Then: the poll never connects and the token-bearing request goes nowhere else.
    assert result.error == ENDPOINT_BLOCKED_MESSAGE
    assert wire.connections == ["8.8.8.8"]
    assert wire.streams[0].request_line == "POST /submissions HTTP/1.1"


async def test_judge0_flow_keeps_payload_polling_and_output_budget(wire: Wire) -> None:
    # Given: a finished submission with oversized output.
    wire.responses = [
        reply(201, {"token": "tok"}),
        reply(200, {"status": {"id": 3}, "stdout": "x" * 20000, "stderr": "e" * 9000, "exit_code": 0}),
    ]
    # When
    result = await build("judge0").execute("print(1)", "python", exec_timeout=5)
    # Then: one field-filtered GET completes the round and terminal status stops polling.
    assert wire.streams[0].body == {
        "source_code": "print(1)",
        "language_id": 71,
        "cpu_time_limit": 5,
        "memory_limit": "256m",
    }
    assert [stream.request_line for stream in wire.streams[1:]] == [
        "GET /submissions/tok?fields=stdout%2Cstderr%2Cstatus%2Ctime%2Cmemory%2Cexit_code HTTP/1.1",
    ]
    assert (result.success, len(result.stdout), len(result.stderr)) == (True, 10000, 5000)


async def test_judge0_default_endpoint_uses_the_public_client(wire: Wire) -> None:
    # Given: no configured endpoint.
    wire.responses = [reply(200, [])]
    backend = Judge0Backend(SandboxConfig(type=SandboxType.JUDGE0))
    # When
    healthy = await backend.health_check()
    # Then: the hosted default is reached through the public transport.
    assert healthy is True
    assert wire.streams[0].tls == ("api.judge0.com", ssl.CERT_REQUIRED, True)


@pytest.mark.parametrize("stage", ["submit", "poll"])
async def test_judge0_shared_deadline_stops_a_stalled_wire(wire: Wire, stage: str) -> None:
    stalled = StalledReply()
    wire.responses = [stalled] if stage == "submit" else [reply(201, {"token": "tok"}), stalled]
    deadline = ExecutionDeadline(0.1, grace=0.2)
    async with asyncio.timeout(1):
        result = await build("judge0").execute("pass", "python", deadline=deadline)
    assert stalled.started.is_set()
    assert stalled.closed.is_set()
    assert result.termination == TerminationReason.SANDBOX_TIMEOUT
    assert result.exit_code == 124
    assert deadline.hard_remaining() > 0
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            reply(200, {"status": {"id": 5, "description": "Time Limit Exceeded"}}),
            TerminationReason.PROVIDER_TIME_LIMIT,
        ),
        (httpcore.ReadError("broken wire"), TerminationReason.BACKEND_ABORT),
        (httpcore.ReadTimeout("read deadline"), TerminationReason.BACKEND_ABORT),
    ],
)
async def test_judge0_distinguishes_provider_time_limit_from_transport_failure(
    wire: Wire, response: bytes | httpcore.NetworkError | httpcore.TimeoutException, reason: TerminationReason
) -> None:
    wire.responses = [reply(201, {"token": "tok"}), response]
    result = await build("judge0").execute("pass", "python")
    assert not result.success
    assert result.termination == reason
    assert len(wire.streams) == 2
    assert all(stream.closed for stream in wire.streams)
