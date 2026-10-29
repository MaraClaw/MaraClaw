import json
import uuid
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.services import agent_manager as agent_manager_module
from app.services.agent_manager import AgentManager
from app.services.guest_admission import GuestStartPreflightError
from test_agent_manager_openclaw_config import GuestSettings, RecordingDockerClient

NAMES = ("EMBEDDING_MODEL", "EMBEDDING_DIMS", "EMBEDDING_PROVIDER_URL", "EMBEDDING_API_KEY")
VALID = {
    "EMBEDDING_MODEL": "text-embedding-3-large",
    "EMBEDDING_DIMS": "1536",
    "EMBEDDING_PROVIDER_URL": "https://embeddings.example/v1",
    "EMBEDDING_API_KEY": "embed-secret-key",
}
COMPLETE_SEARCH = {
    "enabled": True,
    "provider": "openai-compatible",
    "model": "text-embedding-3-large",
    "outputDimensionality": 1536,
    "remote": {"baseUrl": "https://embeddings.example/v1", "apiKey": "embed-secret-key"},
}
INCOMPLETE_SETS = [subset for size in range(len(NAMES)) for subset in combinations(NAMES, size)]


@dataclass(frozen=True, slots=True)
class Guest:
    manager: AgentManager
    settings: GuestSettings
    monkeypatch: pytest.MonkeyPatch
    agent: Any
    directory: Path

    @property
    def config_path(self) -> Path:
        return self.directory / "openclaw.json"

    def configure(self, values: dict[str, str]) -> None:
        for name in NAMES:
            self.monkeypatch.setattr(self.settings, name, values.get(name, ""), raising=False)

    def search(self) -> dict[str, Any]:
        return json.loads(self.config_path.read_text(encoding="utf-8"))["memory"]["search"]

    def fingerprint(self) -> str:
        return self.manager._guest_config_fingerprint(primary=None, secondary=None, fallback=None, selected=None)


@pytest.fixture
def guest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Guest:
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "_guest_config_fingerprints", {})
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(),
        name="Embedding Agent",
        creator_id=uuid.uuid4(),
        primary_model_id=None,
        gogcli_enabled=False,
        setup_pending=False,
    )
    directory = tmp_path / str(agent.id)
    directory.mkdir()
    manager = AgentManager.__new__(AgentManager)

    async def materialize(agent_id: uuid.UUID) -> Path:
        assert agent_id == agent.id
        return directory

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize)
    return Guest(manager, settings, monkeypatch, agent, directory)


@pytest.fixture(autouse=True)
def fake_token_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def prepare(self: AgentManager, agent_id: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(AgentManager, "_prepare_linkup_proxy_token", prepare)


def test_settings_read_exact_names_default_empty_and_ignore_other_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    model_config = Settings.model_config.copy()
    model_config["env_file"] = None
    monkeypatch.setattr(Settings, "model_config", model_config)
    for name in (*NAMES, "EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "99")

    # When
    unset = Settings()
    for name, value in VALID.items():
        monkeypatch.setenv(name, value)
    configured = Settings()

    # Then
    assert [getattr(unset, name, None) for name in NAMES] == ["", "", "", ""]
    assert {name: getattr(configured, name, None) for name in NAMES} == VALID


def test_complete_config_maps_trimmed_values_to_openai_compatible_memory_search(guest: Guest) -> None:
    # Given
    guest.configure({name: f"  {value}\t" for name, value in VALID.items()})

    # When
    config: dict[str, Any] = guest.manager._generate_openclaw_config(guest.agent, model=None)

    # Then
    assert config["memory"]["search"] == COMPLETE_SEARCH
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["plugins"] == {"slots": {"memory": "memory-core"}}


@pytest.mark.parametrize("dimensions", ["1536", "not-a-number"])
@pytest.mark.parametrize("blank", ["", " \t "])
@pytest.mark.parametrize("present", INCOMPLETE_SETS, ids=lambda names: "+".join(n[10:] for n in names) or "none")
def test_any_incomplete_set_uses_upstream_defaults_and_ignores_bad_dimensions(
    guest: Guest, present: tuple[str, ...], blank: str, dimensions: str
) -> None:
    # Given
    values = {name: VALID[name] for name in present}
    if "EMBEDDING_DIMS" in values:
        values["EMBEDDING_DIMS"] = dimensions
    guest.configure({name: values.get(name, blank) for name in NAMES})

    # When
    config: dict[str, Any] = guest.manager._generate_openclaw_config(guest.agent, model=None)

    # Then
    assert config["memory"]["search"] == {"enabled": True}


@pytest.mark.parametrize("dimensions", ["abc", "0", "-1", "1.5", "1e3", "12 34", "+7", "1_0"])
def test_complete_config_with_invalid_dimensions_names_variable_without_values(guest: Guest, dimensions: str) -> None:
    # Given
    guest.configure({**VALID, "EMBEDDING_DIMS": dimensions})

    # When
    with pytest.raises(ValueError, match="EMBEDDING_DIMS") as caught:
        guest.manager._generate_openclaw_config(guest.agent, model=None)

    # Then
    message = str(caught.value)
    assert not any(value in message for name, value in VALID.items() if name != "EMBEDDING_DIMS")
    assert f"'{dimensions}'" not in message
    assert f'"{dimensions}"' not in message


def test_invalid_complete_dimensions_leave_existing_guest_config_and_cache_untouched(guest: Guest) -> None:
    # Given
    guest.config_path.write_text('{"sentinel": true}', encoding="utf-8")
    cache = agent_manager_module._guest_config_fingerprints
    cache[str(guest.agent.id)] = "previous"
    guest.configure({**VALID, "EMBEDDING_DIMS": "zero"})

    # When
    with pytest.raises(ValueError, match="EMBEDDING_DIMS"):
        guest.manager.write_guest_config(guest.agent, primary=None)

    # Then
    assert guest.config_path.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert cache == {str(guest.agent.id): "previous"}


async def test_invalid_complete_dimensions_fail_before_config_overwrite_or_container_replacement(
    guest: Guest,
) -> None:
    # Given
    guest.config_path.write_text('{"sentinel": true}', encoding="utf-8")
    docker = RecordingDockerClient(
        inspect_result=SimpleNamespace(
            id="stale-container",
            name=f"maraclaw-agent-{guest.agent.id}",
            config=SimpleNamespace(labels={"maraclaw.agent_id": str(guest.agent.id)}),
            host_config=SimpleNamespace(port_bindings={}),
        )
    )
    guest.monkeypatch.setattr(guest.manager, "docker_client", docker, raising=False)
    guest.configure({**VALID, "EMBEDDING_DIMS": "-5"})

    # When
    with pytest.raises(GuestStartPreflightError) as caught:
        await guest.manager.replace_container(guest.agent)

    # Then
    assert "EMBEDDING_DIMS" in str(caught.value.__cause__)
    assert guest.config_path.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert docker.container.stop_calls == []
    assert docker.container.remove_calls == []
    assert docker.run_kwargs is None


async def test_new_container_receives_complete_embedding_config_without_guest_env_leak(guest: Guest) -> None:
    # Given
    docker = RecordingDockerClient()
    guest.monkeypatch.setattr(guest.manager, "docker_client", docker, raising=False)
    guest.configure(VALID)

    # When
    container_id = await guest.manager.replace_container(guest.agent)

    # Then
    assert container_id == "container-1234567890"
    assert guest.search() == COMPLETE_SEARCH
    assert docker.run_kwargs is not None
    envs = docker.run_kwargs["envs"]
    assert not any(name.startswith("EMBEDDING_") for name in envs)
    assert VALID["EMBEDDING_API_KEY"] not in envs.values()


def test_embedding_credentials_stay_separate_from_chat_provider_credentials(guest: Guest) -> None:
    # Given
    guest.monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    primary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        model="gpt-6.1-sol",
        api_key_encrypted="chat-secret-key",
        auth_kind="api_key",
        label="Chat",
    )
    guest.configure(VALID)

    # When
    config: dict[str, Any] = guest.manager._generate_openclaw_config(guest.agent, primary, selected=primary)

    # Then
    assert config["env"]["vars"]["OPENAI_API_KEY"] == "chat-secret-key"
    assert "embed-secret-key" not in json.dumps(config["env"])
    assert "EMBEDDING_" not in json.dumps(config["env"])
    assert config["memory"]["search"] == COMPLETE_SEARCH
    assert "chat-secret-key" not in json.dumps(config["memory"])


@pytest.mark.parametrize(
    ("name", "changed"),
    [
        ("EMBEDDING_MODEL", "other-model"),
        ("EMBEDDING_DIMS", "768"),
        ("EMBEDDING_PROVIDER_URL", "https://other.example/v1"),
        ("EMBEDDING_API_KEY", "rotated-secret-key"),
    ],
)
def test_each_embedding_change_rewrites_cached_guest_config(guest: Guest, name: str, changed: str) -> None:
    # Given
    guest.configure(VALID)
    guest.manager.write_guest_config(guest.agent, primary=None)
    guest.config_path.write_text("{}", encoding="utf-8")
    guest.manager.write_guest_config(guest.agent, primary=None)
    assert guest.config_path.read_text(encoding="utf-8") == "{}"

    # When
    guest.configure({**VALID, name: changed})
    guest.manager.write_guest_config(guest.agent, primary=None)

    # Then
    assert guest.search() != COMPLETE_SEARCH
    assert guest.search()["provider"] == "openai-compatible"
    assert changed in json.dumps(guest.search())


@pytest.mark.parametrize("skip_cache", [False, True])
def test_removal_restores_upstream_defaults_in_persisted_config(guest: Guest, skip_cache: bool) -> None:
    # Given
    guest.configure(VALID)
    guest.manager.write_guest_config(guest.agent, primary=None)
    assert guest.search() == COMPLETE_SEARCH
    if skip_cache:
        agent_manager_module._guest_config_fingerprints.clear()

    # When
    guest.configure({**VALID, "EMBEDDING_API_KEY": " "})
    guest.manager.write_guest_config(guest.agent, primary=None)

    # Then
    assert guest.search() == {"enabled": True}


def test_fingerprint_hashes_embedding_values_and_logs_never_contain_them(guest: Guest) -> None:
    # Given
    messages: list[str] = []

    class RecordingLogger:
        def __getattr__(self, _level: str) -> Any:
            return lambda message, *args, **kwargs: messages.append(
                " ".join([str(message), *map(str, args), *map(str, kwargs.values())])
            )

    guest.monkeypatch.setattr(agent_manager_module, "logger", RecordingLogger())
    guest.configure(VALID)

    # When
    first = guest.fingerprint()
    guest.manager.write_guest_config(guest.agent, primary=None)
    guest.configure({**VALID, "EMBEDDING_API_KEY": "rotated-secret-key"})
    rotated = guest.fingerprint()
    guest.manager.write_guest_config(guest.agent, primary=None)
    guest.configure({})
    removed = guest.fingerprint()
    guest.manager.write_guest_config(guest.agent, primary=None)

    # Then
    assert len({first, rotated, removed}) == 3
    leaked = [*VALID.values(), "rotated-secret-key"]
    assert not any(secret in text for secret in leaked for text in (first, rotated, removed, *messages))
