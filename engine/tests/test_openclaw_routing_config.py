import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.core.security import encrypt_data
from app.records.agent import AgentRecord
from app.records.llm import LLMModelRecord
from app.services import agent_manager as manager_module
from app.services.agent_manager import AgentManager, guest_model_ref
from app.services.llm.turn import ModelBundle
from app.services.openclaw_guest_config import GuestConfigWriteError, apply_guest_model


def model(name: str, provider: str = "anthropic", key: str = "sk-test") -> LLMModelRecord:
    return LLMModelRecord(uuid4(), provider, name, encrypt_data(key, get_settings().SECRET_KEY), name)


@pytest.mark.parametrize(
    ("provider", "name", "expected"),
    [
        ("openai", "", None),
        ("openai", "gpt-5.4", "openai/gpt-5.4"),
        ("grok", "grok-4.8", "xai/grok-4.8"),
        ("xai", "auto", "xai/auto"),
    ],
)
def test_guest_model_ref(provider: str, name: str, expected: str | None) -> None:
    assert guest_model_ref(model(name, provider)) == expected
    assert guest_model_ref(None) is None


def test_config_defaults_to_primary_and_registers_all_models() -> None:
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary, secondary, fallback = model("opus", key="sk-opus"), model("haiku"), model("gpt-5.4", "openai", "sk-gpt")
    config = manager._generate_openclaw_config(agent, primary, secondary=secondary, fallback=fallback)
    agents = config["agents"]
    assert isinstance(agents, dict)
    defaults = agents["defaults"]
    assert isinstance(defaults, dict)
    assert defaults["model"] == {"primary": "anthropic/opus", "fallbacks": ["openai/gpt-5.4"]}
    assert defaults["models"] == {
        "anthropic/opus": {"alias": "primary"},
        "anthropic/haiku": {"alias": "secondary"},
        "openai/gpt-5.4": {"alias": "fallback"},
    }
    environment = config["env"]
    assert isinstance(environment, dict)
    variables = environment["vars"]
    assert isinstance(variables, dict)
    assert variables["ANTHROPIC_API_KEY"] == "sk-opus"
    assert variables["OPENAI_API_KEY"] == "sk-gpt"


@pytest.mark.parametrize(
    ("setting", "value", "path"),
    [
        ("OPENCLAW_THINKING_DEFAULT", "low", "thinkingDefault"),
        ("OPENCLAW_BOOTSTRAP_MAX_CHARS", 1000, "bootstrapMaxChars"),
        ("OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS", 2000, "bootstrapTotalMaxChars"),
        ("OPENCLAW_ANTHROPIC_CACHE_RETENTION", "long", "cacheRetention"),
    ],
)
def test_experiment_rewrites_and_unset_respects_guest_ownership(
    manager: AgentManager, monkeypatch: pytest.MonkeyPatch, setting: str, value: str | int, path: str
) -> None:
    monkeypatch.setattr(manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary, secondary = model("opus"), model("gpt-5.4", "openai")
    config_path = manager.write_guest_config(agent, primary=primary, secondary=secondary)
    assert config_path is not None
    baseline = config_path.read_bytes()
    monkeypatch.setattr(manager_module.settings, setting, value)
    manager.write_guest_config(agent, primary=primary, secondary=secondary)
    defaults = json.loads(config_path.read_bytes())["agents"]["defaults"]
    if path == "cacheRetention":
        assert defaults["models"]["anthropic/opus"]["params"][path] == value
        assert "params" not in defaults["models"]["openai/gpt-5.4"]
    else:
        assert defaults[path] == value
    monkeypatch.setattr(manager_module.settings, setting, None)
    manager.write_guest_config(agent, primary=primary, secondary=secondary)
    if path == "cacheRetention":
        assert config_path.read_bytes() == baseline
    else:
        assert json.loads(config_path.read_bytes())["agents"]["defaults"][path] == value


@pytest.mark.parametrize(
    ("setting", "path"),
    [
        ("OPENCLAW_THINKING_DEFAULT", "thinkingDefault"),
        ("OPENCLAW_BOOTSTRAP_MAX_CHARS", "bootstrapMaxChars"),
        ("OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS", "bootstrapTotalMaxChars"),
    ],
)
def test_explicit_experiment_overrides_existing_guest_default(
    manager: AgentManager, monkeypatch: pytest.MonkeyPatch, setting: str, path: str
) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    custom = "high" if path == "thinkingDefault" else 12000
    override = "off" if path == "thinkingDefault" else 1000
    config_path = manager._agent_dir(agent.id) / "openclaw.json"
    config_path.write_text(json.dumps({"agents": {"defaults": {path: custom}}}), encoding="utf-8")
    monkeypatch.setattr(manager_module.settings, setting, None)
    manager.write_guest_config(agent, primary=None)
    assert json.loads(config_path.read_bytes())["agents"]["defaults"][path] == custom

    monkeypatch.setattr(manager_module.settings, setting, override)
    manager.write_guest_config(agent, primary=None)
    assert json.loads(config_path.read_bytes())["agents"]["defaults"][path] == override


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("OPENCLAW_THINKING_DEFAULT", "invalid"),
        ("OPENCLAW_ANTHROPIC_CACHE_RETENTION", "short"),
        ("OPENCLAW_BOOTSTRAP_MAX_CHARS", 0),
        ("OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS", -1),
    ],
)
def test_experiment_settings_reject_invalid_values(setting, value):
    with pytest.raises(ValidationError):
        type(get_settings()).model_validate({**get_settings().model_dump(), setting: value})


def test_unset_experiments_keep_original_generated_json_bytes(monkeypatch):
    settings = manager_module.settings
    for name in ("EMBEDDING_MODEL", "EMBEDDING_DIMS", "EMBEDDING_PROVIDER_URL", "EMBEDDING_API_KEY"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(settings, "OPENCLAW_ENGINE_BASE_URL", "http://maraclaw-engine:8000")
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    actual = AgentManager.__new__(AgentManager)._generate_openclaw_config(agent, None, gateway_token="test-token")
    expected = {
        "agents": {
            "defaults": {
                "workspace": "${OPENCLAW_STATE_DIR}/workspace",
                "model": {"primary": "openai/gpt-6.1-sol"},
                "modelPolicy": {"allow": ["openai/gpt-6.1-sol"]},
            }
        },
        "memory": {"search": {"enabled": True}},
        "plugins": {"slots": {"memory": "memory-core"}},
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "port": 18789,
            "auth": {"mode": "token", "token": "test-token"},
            "controlUi": {"allowedOrigins": ["http://localhost:18789", "http://127.0.0.1:18789"]},
        },
        "env": {"vars": {"MARACLAW_API_BASE": "http://maraclaw-engine:8000"}},
        "skills": {
            "load": {"extraDirs": ["/home/node/.openclaw/skills"]},
            "entries": {"linkup-search": {"enabled": False, "env": {}}, "linkup-fetch": {"enabled": False, "env": {}}},
        },
        "tools": {"deny": ["web_search"], "web": {"search": {"enabled": False}}},
    }
    assert json.dumps(actual, indent=2).encode() == json.dumps(expected, indent=2).encode()


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AgentManager:
    value = AgentManager.__new__(AgentManager)
    monkeypatch.setattr(value, "_agent_dir", lambda _id: tmp_path)
    manager_module._guest_config_fingerprints.clear()
    return value


def test_write_skips_missing_directory(manager: AgentManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "_agent_dir", lambda _id: tmp_path / "missing")
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    assert manager.write_guest_config(agent, primary=None) is None


def test_write_preserves_unchanged_config(manager: AgentManager) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary = model("opus")
    path = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert path is not None
    path.write_text("sentinel-unchanged", encoding="utf-8")
    assert manager.write_guest_config(agent, primary=primary, selected=primary) == path
    assert path.read_text(encoding="utf-8") == "sentinel-unchanged"


def test_write_rotates_credentials_without_changing_model(manager: AgentManager) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary = model("opus", key="sk-old")
    path = manager.write_guest_config(agent, primary=primary, selected=primary)
    assert path is not None
    rotated = replace(primary, api_key_encrypted=encrypt_data("sk-new", get_settings().SECRET_KEY))
    assert manager.write_guest_config(agent, primary=rotated, selected=rotated) == path
    assert "sk-new" in path.read_text(encoding="utf-8")


def test_write_lists_every_slot_and_defaults_to_primary(manager: AgentManager) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary, secondary = model("opus"), model("haiku")
    path = manager.write_guest_config(agent, primary=primary, secondary=secondary, selected=secondary)
    assert path is not None
    written = path.read_text(encoding="utf-8")
    assert '"primary": "anthropic/opus"' in written
    assert '"alias": "secondary"' in written


def test_alternating_selected_slots_write_the_config_once(
    manager: AgentManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary, secondary = model("opus"), model("haiku")
    writes = Mock(wraps=manager._atomic_write_json)
    monkeypatch.setattr(manager, "_atomic_write_json", writes)
    path = manager.write_guest_config(agent, primary=primary, secondary=secondary, selected=primary)
    assert path is not None
    for selected in (secondary, primary, secondary):
        assert manager.write_guest_config(agent, primary=primary, secondary=secondary, selected=selected) == path
    openclaw_writes = [call for call in writes.call_args_list if call.args[0] == path]
    assert len(openclaw_writes) == 1
    assert '"primary": "anthropic/opus"' in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "failure", [OSError("private path"), ValueError("private config"), RuntimeError("private bug")]
)
def test_config_write_failure_preserves_private_cause_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary = model("opus")
    monkeypatch.setattr(manager_module.agent_manager, "write_guest_config", Mock(side_effect=failure))
    with pytest.raises(GuestConfigWriteError) as raised:
        apply_guest_model(agent, ModelBundle(primary=primary), primary)
    assert raised.value.__cause__ is failure
    assert "private" not in str(raised.value)
