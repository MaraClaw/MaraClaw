import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, assert_never
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.json_types import JsonObject, json_loads_object, json_object_from
from app.records.agent import AgentRecord
from app.services import agent_manager as module
from app.services.agent_manager import AgentManager, guest_container_name
from app.services.guest_admission import GuestStartPreflightError
from test_agent_manager_openclaw_config import RecordingDockerClient

type Writer = Literal["refresh", "recreate"]


@dataclass(frozen=True, slots=True)
class Guest:
    manager: AgentManager
    agent: AgentRecord
    path: Path
    docker: RecordingDockerClient


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Guest:
    monkeypatch.setattr(module.settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(module.settings, "OPENCLAW_MEMORY_TENCENTDB_ENABLED", False)
    monkeypatch.setattr(module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(module, "_guest_config_fingerprints", {})
    agent = AgentRecord(id=uuid4(), name="Resource Agent", creator_id=uuid4())
    directory = tmp_path / str(agent.id)
    directory.mkdir()
    manager = AgentManager.__new__(AgentManager)
    docker = RecordingDockerClient()
    monkeypatch.setattr(manager, "docker_client", docker, raising=False)

    async def materialize(agent_id: UUID) -> Path:
        assert agent_id == agent.id
        return directory

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize)
    return Guest(manager, agent, directory / "openclaw.json", docker)


async def rewrite(guest: Guest, writer: Writer) -> None:
    if writer == "recreate":
        await guest.manager.start_container(guest.agent)
    else:
        guest.manager.write_guest_config(guest.agent, primary=None)


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
@pytest.mark.parametrize(
    ("section", "resource"),
    [
        ("mcp", {"servers": {"local": {"command": "node", "args": ["server.mjs"], "enabled": False}}}),
        (
            "skills",
            {
                "load": {"extraDirs": ["/home/node/.openclaw/custom-skills"]},
                "entries": {"custom": {"enabled": False, "env": {"RESOURCE_KEY": "retained"}}},
            },
        ),
        ("plugins", {"entries": {"custom": {"enabled": True, "config": {"endpoint": "http://local"}}}}),
    ],
)
async def test_guest_resource_settings_survive_config_rewrite(
    guest: Guest, writer: Writer, section: str, resource: JsonObject
) -> None:
    # Given
    guest.path.write_text(json.dumps({section: resource}), encoding="utf-8")

    # When
    await rewrite(guest, writer)

    # Then
    stored = json_object_from(json_loads_object(guest.path.read_text(encoding="utf-8"))[section])
    for key, value in resource.items():
        if key == "entries":
            assert json_object_from(stored[key]).get("custom") == json_object_from(value)["custom"]
        else:
            assert stored[key] == value


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
async def test_platform_config_wins_over_stale_guest_settings(guest: Guest, writer: Writer) -> None:
    # Given
    guest.path.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"model": {"primary": "stale/model"}, "memorySearch": {"enabled": True}}},
                "env": {"vars": {"OPENAI_API_KEY": "stale"}},
                "auth": {"profiles": {"stale": {"provider": "openai", "mode": "oauth"}}},
                "tools": {"deny": [], "web": {"search": {"enabled": True}}},
                "gateway": {"mode": "remote"},
                "skills": {"entries": {"linkup-search": {"enabled": False, "env": {"LINKUP_API_KEY": "stale"}}}},
                "plugins": {
                    "enabled": False,
                    "slots": {"memory": "stale"},
                    "entries": {"memory-tencentdb": {"enabled": True}, "custom": {"enabled": True}},
                },
            }
        ),
        encoding="utf-8",
    )
    generated = guest.manager._generate_openclaw_config(guest.agent, None)

    # When
    await rewrite(guest, writer)

    # Then
    stored = json_loads_object(guest.path.read_text(encoding="utf-8"))
    for key in ("agents", "env", "tools", "gateway"):
        assert stored[key] == generated[key]
    assert "auth" not in stored
    assert stored["skills"] == generated["skills"]
    assert stored["plugins"] == {"entries": {"custom": {"enabled": True}}}


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
@pytest.mark.parametrize("text", ["{broken", "[]", '{"skills":{"entries":[]}}'])
async def test_corrupt_guest_resource_config_is_not_overwritten(guest: Guest, writer: Writer, text: str) -> None:
    # Given
    guest.path.write_text(text, encoding="utf-8")
    guest.agent.status = "running"
    guest.agent.container_id = "old-guest"
    guest.agent.container_port = 19876
    old_guest = SimpleNamespace(
        id="old-guest",
        name=guest_container_name(guest.agent.id),
        config=SimpleNamespace(labels={"maraclaw.agent_id": str(guest.agent.id)}),
        host_config=SimpleNamespace(port_bindings={}),
    )
    guest.docker.container.inspect_result = old_guest
    match writer:
        case "refresh":
            expected_error = ValidationError
        case "recreate":
            expected_error = GuestStartPreflightError
        case unreachable:
            assert_never(unreachable)

    # When
    with pytest.raises(expected_error):
        await rewrite(guest, writer)

    # Then
    assert guest.path.read_text(encoding="utf-8") == text
    assert (guest.agent.status, guest.agent.container_id, guest.agent.container_port) == ("running", "old-guest", 19876)
    assert guest.docker.container.stop_calls == []
    assert guest.docker.container.remove_calls == []
    assert guest.docker.run_args is None
    assert guest.docker.container.inspect_result is old_guest
