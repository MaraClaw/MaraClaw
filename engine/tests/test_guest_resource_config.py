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
from app.services.guest_resource_config import preserve_guest_resources
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


@pytest.mark.parametrize(("section", "key"), [("mcp", "servers"), ("skills", "entries"), ("plugins", "entries")])
def test_generated_resource_entries_win_without_dropping_custom_entries(tmp_path: Path, section: str, key: str) -> None:
    # Given
    path = tmp_path / "openclaw.json"
    path.write_text(
        json.dumps({section: {key: {"managed": {"enabled": False}, "custom": {"enabled": False}}}}),
        encoding="utf-8",
    )
    generated: JsonObject = {section: {key: {"managed": {"enabled": True}, "new": {"enabled": True}}}}

    # When
    result = preserve_guest_resources(path, generated)

    # Then
    assert result == {
        section: {key: {"custom": {"enabled": False}, "managed": {"enabled": True}, "new": {"enabled": True}}}
    }


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
                    "entries": {"custom": {"enabled": True}},
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
    assert stored["plugins"] == {
        "slots": {"memory": "memory-core"},
        "entries": {"custom": {"enabled": True}},
    }


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
@pytest.mark.parametrize("config_exists", [False, True])
async def test_native_memory_and_custom_data_survive_config_rewrites(
    guest: Guest, writer: Writer, config_exists: bool
) -> None:
    # Given
    saved = {
        "workspace/MEMORY.md": b"Project codename: orchid.\n",
        "workspace/memory/2026-10-23.md": b"Keep daily memory.\n",
        "MEMORY.md": b"Keep root memory.\n",
        "memory/archive.md": b"Keep root memory directory.\n",
        "memory/main.sqlite": b"SQLite format 3\x00native-state\xff",
        "extensions/custom/index.js": b"export default {};\n",
        "extensions/custom/data/memory.sqlite": b"SQLite format 3\x00custom-state\xff",
    }
    for relative, content in saved.items():
        path = guest.path.parent / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    previous_entries: JsonObject = {"custom": {"enabled": True, "config": {"keep": "custom-value"}}}
    if config_exists:
        guest.path.write_text(json.dumps({"plugins": {"entries": previous_entries}}), encoding="utf-8")

    # When: cold refresh, cached refresh, and recreation must preserve native and custom data.
    await rewrite(guest, writer)
    guest.manager.write_guest_config(guest.agent, primary=None)
    module._guest_config_fingerprints.clear()
    await rewrite(guest, writer)
    guest.manager.write_guest_config(guest.agent, primary=None)

    # Then
    stored = json_loads_object(guest.path.read_text(encoding="utf-8"))
    assert json_object_from(stored["memory"])["search"] == {"enabled": True, "provider": "none"}
    plugins: JsonObject = {"slots": {"memory": "memory-core"}}
    if config_exists:
        plugins["entries"] = previous_entries
    assert stored["plugins"] == plugins
    assert {relative: (guest.path.parent / relative).read_bytes() for relative in saved} == saved


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
