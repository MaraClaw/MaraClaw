from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

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


def test_config_registers_all_models_and_selected_secondary() -> None:
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary, secondary, fallback = model("opus", key="sk-opus"), model("haiku"), model("gpt-5.4", "openai", "sk-gpt")
    config = manager._generate_openclaw_config(
        agent, primary, secondary=secondary, fallback=fallback, selected=secondary
    )
    agents = config["agents"]
    assert isinstance(agents, dict)
    defaults = agents["defaults"]
    assert isinstance(defaults, dict)
    assert defaults["model"] == {"primary": "anthropic/haiku", "fallbacks": ["openai/gpt-5.4"]}
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


def test_write_changes_selected_model(manager: AgentManager) -> None:
    agent = AgentRecord(id=uuid4(), name="Guest", creator_id=uuid4())
    primary, secondary = model("opus"), model("haiku")
    path = manager.write_guest_config(agent, primary=primary, secondary=secondary, selected=secondary)
    assert path is not None
    written = path.read_text(encoding="utf-8")
    assert "anthropic/haiku" in written
    assert '"alias": "secondary"' in written


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
