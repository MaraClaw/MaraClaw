import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, assert_never
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.core.domain.agents import AgentStatus
from app.core.json_types import JsonObject, json_loads_object, json_object_from
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
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
    for name in ("EMBEDDING_MODEL", "EMBEDDING_DIMS", "EMBEDDING_PROVIDER_URL", "EMBEDDING_API_KEY"):
        monkeypatch.setattr(module.settings, name, "")
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
        await guest.manager.replace_container(guest.agent)
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
                "load": {"extraDirs": ["/home/node/.openclaw/custom-skills", "/home/node/.openclaw/skills"]},
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
        elif section == "skills" and key == "load":
            assert stored[key] == {"extraDirs": ["${OPENCLAW_STATE_DIR}/custom-skills", "/home/node/.openclaw/skills"]}
        else:
            assert stored[key] == value


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
@pytest.mark.parametrize(
    ("root", "resolved"),
    [
        ("/custom/skills", "/custom/skills"),
        ("/home/node/.openclaw/custom-skills", "${OPENCLAW_STATE_DIR}/custom-skills"),
        ("/home/node/custom-state/custom-skills", "${OPENCLAW_STATE_DIR}/custom-skills"),
    ],
)
async def test_image_skill_discovery_is_added_without_dropping_custom_roots(
    guest: Guest, writer: Writer, root: str, resolved: str
) -> None:
    guest.path.write_text(json.dumps({"skills": {"load": {"extraDirs": [root, root]}}}), encoding="utf-8")

    await rewrite(guest, writer)

    stored = json_loads_object(guest.path.read_text(encoding="utf-8"))
    assert json_object_from(json_object_from(stored["skills"])["load"])["extraDirs"] == [
        resolved,
        "/home/node/.openclaw/skills",
    ]


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


GUEST_DEFAULTS: JsonObject = {"modelPolicy": {"mode": "strict", "allow": ["openai/*"]}, "thinkingDefault": "low"}
GUEST_ENTRIES: JsonObject = {
    "main": {"identity": {"name": "Ada", "emoji": "A"}, "skills": ["custom"], "modelPolicy": {"mode": "strict"}}
}


def test_guest_owned_fields_survive_while_stale_platform_fields_are_replaced(tmp_path: Path) -> None:
    # Given: guest-owned values sit beside stale values at every platform-generated path
    path = tmp_path / "openclaw.json"
    stale_entry: JsonObject = {
        **json_object_from(GUEST_ENTRIES["main"]),
        "model": "retired/model",
        "modelPolicy": {"mode": "strict", "allow": ["retired/*"]},
    }
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        **GUEST_DEFAULTS,
                        "workspace": "/stale/workspace",
                        "model": {"primary": "stale/model", "fallbacks": ["retired/model"]},
                        "models": {"retired/model": {"alias": "fallback"}},
                        "memorySearch": {"remote": {"apiKey": "stale-embedding-key"}},
                        "heartbeat": {
                            "every": "30s",
                            "prompt": "Check the MaraClaw inbox with the maraclaw-sync skill",
                        },
                    },
                    "entries": {"main": stale_entry},
                },
                "channels": {"slack": {"enabled": True}},
                "memory": {"citations": "off", "search": {"remote": {"apiKey": "stale-embedding-key"}}},
                "plugins": {"enabled": False, "slots": {"memory": "stale"}, "load": {"paths": ["/custom/plugin"]}},
                "models": {"providers": {"retired": {"apiKey": "stale-provider-key"}}},
                "env": {"vars": {"RETIRED_API_KEY": "stale-provider-key"}},
                "auth": {"profiles": {"retired": {"provider": "retired", "mode": "oauth"}}},
                "gateway": {"auth": {"token": "stale-token"}},
                "tools": {"deny": []},
                "hooks": {"enabled": True, "path": "/hooks", "token": "retired-token"},
            }
        ),
        encoding="utf-8",
    )
    platform_defaults: JsonObject = {
        "workspace": "/workspace",
        "model": {"primary": "new/model"},
        "modelPolicy": {"allow": ["new/model"]},
    }
    generated: JsonObject = {
        "agents": {"defaults": platform_defaults},
        "memory": {"search": {"enabled": True}},
        "plugins": {"slots": {"memory": "memory-core"}},
        "env": {"vars": {"NEW_API_KEY": "fresh"}},
        "gateway": {"auth": {"token": "fresh-token"}},
        "tools": {"deny": ["web_search"]},
    }

    # When
    result = preserve_guest_resources(path, generated)

    # Then
    assert result == {
        "agents": {
            "defaults": {
                **GUEST_DEFAULTS,
                **platform_defaults,
                "modelPolicy": {"mode": "strict", "allow": ["new/model"]},
            },
            "entries": GUEST_ENTRIES,
        },
        "channels": {"slack": {"enabled": True}},
        "memory": {"citations": "off", "search": {"enabled": True}},
        "plugins": {"load": {"paths": ["/custom/plugin"]}, "slots": {"memory": "memory-core"}},
        "env": generated["env"],
        "gateway": generated["gateway"],
        "tools": generated["tools"],
    }


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
async def test_guest_agent_settings_survive_regeneration_beside_platform_model(guest: Guest, writer: Writer) -> None:
    # Given
    guest.path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {**GUEST_DEFAULTS, "model": {"primary": "stale/model"}},
                    "entries": GUEST_ENTRIES,
                },
                "env": {"vars": {"RETIRED_API_KEY": "stale"}},
            }
        ),
        encoding="utf-8",
    )
    generated = guest.manager._generate_openclaw_config(guest.agent, None)

    # When
    await rewrite(guest, writer)

    # Then
    stored = json_loads_object(guest.path.read_text(encoding="utf-8"))
    agents = json_object_from(stored["agents"])
    defaults = json_object_from(agents["defaults"])
    assert agents["entries"] == GUEST_ENTRIES
    assert defaults["thinkingDefault"] == GUEST_DEFAULTS["thinkingDefault"]
    assert defaults["modelPolicy"] == {"mode": "strict", "allow": ["openai/gpt-6.1-sol"]}
    assert defaults["model"] == json_object_from(json_object_from(generated["agents"])["defaults"])["model"]
    assert stored["env"] == generated["env"]


def test_guest_agent_settings_survive_model_switch_and_retired_provider_is_removed(
    guest: Guest, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a guest running on provider A whose agent later customised its own settings
    monkeypatch.setattr(module, "get_model_api_key", lambda row: row.api_key_encrypted)

    def model(provider: str, name: str) -> LLMModelRecord:
        return LLMModelRecord(
            id=uuid4(), provider=provider, model=name, api_key_encrypted=f"{provider}-secret", label=name
        )

    old, new = model("openai", "gpt-old"), model("anthropic", "claude-new")
    _ = guest.manager.write_guest_config(guest.agent, primary=old, selected=old)
    config = json_loads_object(guest.path.read_text(encoding="utf-8"))
    written = json_object_from(json_object_from(config["agents"])["defaults"])
    config["agents"] = {"defaults": {**written, **GUEST_DEFAULTS}, "entries": GUEST_ENTRIES}
    _ = guest.path.write_text(json.dumps(config), encoding="utf-8")

    # When
    _ = guest.manager.write_guest_config(guest.agent, primary=new, selected=new)

    # Then
    stored = json_loads_object(guest.path.read_text(encoding="utf-8"))
    agents = json_object_from(stored["agents"])
    defaults = json_object_from(agents["defaults"])
    assert agents["entries"] == GUEST_ENTRIES
    assert defaults["thinkingDefault"] == GUEST_DEFAULTS["thinkingDefault"]
    assert defaults["modelPolicy"] == {"mode": "strict", "allow": ["anthropic/claude-new"]}
    assert defaults["model"] == {"primary": "anthropic/claude-new"}
    assert defaults["models"] == {"anthropic/claude-new": {"alias": "primary"}}
    assert json_object_from(json_object_from(stored["env"])["vars"]).get("ANTHROPIC_API_KEY") == "anthropic-secret"
    assert "OPENAI_API_KEY" not in json_object_from(json_object_from(stored["env"])["vars"])


@pytest.mark.parametrize("duplicate", [False, True])
def test_assigned_openrouter_slots_are_exactly_authorized(guest: Guest, duplicate: bool) -> None:
    names = ["nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3.5-super-vl:free", "recursive/flint-v1.0:free"]
    models = [
        LLMModelRecord(id=uuid4(), provider="openrouter", model=name, label=name, api_key_encrypted="")
        for name in names
    ]
    if duplicate:
        models[2] = models[1]

    config = guest.manager._generate_openclaw_config(guest.agent, models[0], secondary=models[1], fallback=models[2])

    defaults = json_object_from(json_object_from(config["agents"])["defaults"])
    expected = [f"openrouter/{name}" for name in (names[:2] if duplicate else names)]
    assert json_object_from(defaults["modelPolicy"])["allow"] == expected


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
    assert json_object_from(stored["memory"])["search"] == {"enabled": True}
    plugins: JsonObject = {"slots": {"memory": "memory-core"}}
    if config_exists:
        plugins["entries"] = previous_entries
    assert stored["plugins"] == plugins
    assert {relative: (guest.path.parent / relative).read_bytes() for relative in saved} == saved


@pytest.mark.parametrize("writer", ["refresh", "recreate"])
@pytest.mark.parametrize(
    "text", ["{broken", "[]", '{"skills":{"entries":[]}}', '{"agents":[]}', '{"agents":{"entries":{"main":[]}}}']
)
async def test_corrupt_guest_resource_config_is_not_overwritten(guest: Guest, writer: Writer, text: str) -> None:
    # Given
    guest.path.write_text(text, encoding="utf-8")
    guest.agent.status = AgentStatus.RUNNING
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
