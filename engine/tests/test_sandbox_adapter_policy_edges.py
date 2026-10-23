import asyncio
import uuid
from pathlib import Path

import pytest
from tests.sandbox_wire_fakes import (
    Wire,
    build,
    install_wire,
    reply,
)

from app.services import agent_tools
from app.services.sandbox.config import ENDPOINT_BLOCKED_MESSAGE, SandboxConfig


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    return install_wire(monkeypatch)


@pytest.mark.parametrize(
    ("url", "paths"),
    [
        ("https://box.example", ["/health"]),
        ("https://box.example/", ["/health"]),
        ("https://box.example:8443/exec", ["/health"]),
        ("https://box.example/api/exec", ["/api/health"]),
        ("https://box.example/v1/shell/exec", ["/v1/sandbox", "/health"]),
    ],
)
async def test_self_hosted_health_probes_keep_the_configured_origin(wire: Wire, url: str, paths: list[str]) -> None:
    # Given: probes that fail until the last candidate answers.
    wire.responses = [reply(404)] * (len(paths) - 1) + [reply(200, {})]
    host = url.split("/")[2]
    # When
    healthy = await build("self_hosted", url).health_check()
    # Then: every probe stays on the configured host and port.
    assert healthy is True
    assert [stream.request_line for stream in wire.streams] == [f"GET {path} HTTP/1.1" for path in paths]
    assert all(f"Host: {host}\r\n".encode() in stream.written for stream in wire.streams)


async def test_policy_rejection_never_falls_back_to_host_execution(
    wire: Wire, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given: a tenant sandbox endpoint that resolves to loopback and a host launcher that must not run.
    launches: list[bool] = []

    async def launch(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        launches.append(True)
        raise AssertionError("host execution is forbidden")

    async def tool_config(agent_id: uuid.UUID | None, tool_name: str) -> dict[str, str]:
        return {"sandbox_type": "self_hosted", "api_url": "https://box.tenant.example/exec"}

    wire.answers = ["127.0.0.1"]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", tool_config)
    monkeypatch.setattr("app.config.get_sandbox_config", lambda: SandboxConfig(api_key="platform-secret"))
    # When
    result = await agent_tools._execute_code(None, tmp_path, {"code": "print(1)"})
    # Then: the fixed policy error surfaces without addresses, keys or a host launch.
    assert ENDPOINT_BLOCKED_MESSAGE in result
    assert "127.0.0.1" not in result
    assert "platform-secret" not in result
    assert launches == []
    assert wire.connections == []
