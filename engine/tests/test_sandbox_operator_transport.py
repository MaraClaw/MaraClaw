import ssl

import pytest
from tests.sandbox_wire_fakes import (
    KEY,
    KINDS,
    Wire,
    build,
    install_wire,
    queue,
    reply,
    run,
    succeeded,
)

from app.core.outbound_policy import OutboundPolicyError
from app.services.sandbox.config import SandboxConfig, SandboxType
from app.services.sandbox.remote.aio_sandbox_backend import AioSandboxBackend


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    return install_wire(monkeypatch)


@pytest.mark.parametrize("operation", ["health", "execute"])
@pytest.mark.parametrize("kind", KINDS)
async def test_operator_endpoint_may_use_a_private_address_without_proxies(
    wire: Wire, kind: str, operation: str
) -> None:
    # Given: an operator-granted private endpoint and proxy variables in the environment.
    backend = build(kind, "http://10.0.0.5:8080/v1/shell/exec", operator=True)
    expected = queue(wire, kind, operation)
    # When
    result = await run(backend, operation)
    # Then: it connects to the configured host directly, with the key, and closes every stream.
    assert succeeded(result)
    assert wire.connections == ["10.0.0.5"] * expected
    assert wire.lookups == 0
    assert all(stream.closed for stream in wire.streams)
    if operation == "execute" and kind != "judge0":
        assert f"Authorization: Bearer {KEY}".encode() in wire.streams[0].written


async def test_operator_endpoint_still_verifies_tls_for_its_host(wire: Wire) -> None:
    # Given: an operator-granted https endpoint.
    wire.responses = [reply(200, {})]
    backend = build("aio_sandbox", "https://sandbox.internal.example:8443", operator=True)
    # When
    healthy = await backend.health_check()
    # Then: the certificate is verified against the configured host.
    assert healthy is True
    assert wire.connections == ["sandbox.internal.example"]
    assert wire.streams[0].tls == ("sandbox.internal.example", ssl.CERT_REQUIRED, True)


@pytest.mark.parametrize("target", ["http://10.0.0.6:8080/x", "http://10.0.0.5:9090/x", "https://10.0.0.5:8080/x"])
async def test_operator_client_cannot_be_pointed_at_another_origin(wire: Wire, target: str) -> None:
    # Given: an operator-granted origin.
    config = SandboxConfig(type=SandboxType.SELF_HOSTED, api_url="http://10.0.0.5:8080/exec").grant_operator_endpoint()
    # When / Then: any other origin is refused before a connection or credential leaves.
    async with config.http_client(base_url=config.api_url, timeout=1) as client:
        with pytest.raises(OutboundPolicyError):
            _ = await client.get(target, headers={"Authorization": f"Bearer {KEY}"})
    assert wire.connections == []


async def test_operator_authority_does_not_follow_a_substituted_endpoint(wire: Wire) -> None:
    # Given: authority granted for one origin, then an adapter built for another private origin.
    granted = SandboxConfig(type=SandboxType.AIO_SANDBOX, api_url="http://10.0.0.5:8080").grant_operator_endpoint()
    substituted = granted.model_copy(update={"api_url": "http://10.0.0.7:8080"})
    wire.responses = [reply(200, {})]
    # When
    healthy = await AioSandboxBackend(substituted).health_check()
    # Then: the public policy applies and the private address is not dialed.
    assert healthy is False
    assert wire.connections == []


@pytest.mark.parametrize("operator", [False, True])
@pytest.mark.parametrize("kind", ["self_hosted", "aio_sandbox"])
async def test_redirects_are_returned_not_followed(wire: Wire, kind: str, operator: bool) -> None:
    # Given: an endpoint that redirects to a loopback address.
    url = "http://10.0.0.5:8080/v1/shell/exec" if operator else None
    wire.responses = [reply(302, location="http://127.0.0.1:1/steal"), reply(200, {"success": True})]
    # When
    result = await build(kind, url, operator=operator).execute("print(1)", "bash", exec_timeout=5)
    # Then: the redirect is surfaced as a failed response and only one request is sent.
    assert (result.success, result.exit_code) == (False, 302)
    assert len(wire.connections) == 1
