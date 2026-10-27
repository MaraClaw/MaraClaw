import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from pywhales.exceptions import NoSuchContainer

from app.config import Settings
from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.services import agent_manager as agent_manager_module
from app.services.agent_manager import (
    OPENAI_OAUTH_PROFILE_ID,
    XAI_OAUTH_PROFILE_ID,
    AgentManager,
)


class RecordingContainerApi:
    def __init__(self, inspect_result=None, inspect_error=None, stop_error=None, remove_error=None):
        self.inspect_result = inspect_result
        self.inspect_error = inspect_error
        self.stop_error = stop_error
        self.remove_error = remove_error
        self.inspect_calls = []
        self.stop_calls = []
        self.remove_calls = []

    def inspect(self, container_id):
        self.inspect_calls.append(container_id)
        if self.inspect_error is not None:
            raise self.inspect_error
        if self.inspect_result is None:
            raise NoSuchContainer(["docker", "inspect", container_id], 1)
        if hasattr(self.inspect_result, "id") and container_id not in (
            self.inspect_result.id,
            self.inspect_result.name,
        ):
            raise NoSuchContainer(["docker", "inspect", container_id], 1)
        return self.inspect_result

    def stop(self, container_id, **kwargs):
        self.stop_calls.append((container_id, kwargs))
        if self.stop_error is not None:
            raise self.stop_error

    def remove(self, containers):
        self.remove_calls.append(containers)
        if self.remove_error is not None:
            raise self.remove_error


class RecordingDockerClient:
    def __init__(self, inspect_result=None, inspect_error=None, stop_error=None, remove_error=None, run_error=None):
        self.container = RecordingContainerApi(
            inspect_result=inspect_result,
            inspect_error=inspect_error,
            stop_error=stop_error,
            remove_error=remove_error,
        )
        self.run_error = run_error
        self.run_args = None
        self.run_kwargs = None

    def run(self, *args, **kwargs):
        self.run_args = args
        self.run_kwargs = kwargs
        if self.run_error is not None:
            raise self.run_error
        return SimpleNamespace(id="container-1234567890")


class GuestSettings:
    def __init__(self, wrapped_settings: Settings, storage_root: str | None = None):
        self.wrapped_settings = wrapped_settings
        self.STORAGE_LOCAL_ROOT = storage_root if storage_root is not None else wrapped_settings.STORAGE_LOCAL_ROOT

    def __getattr__(self, name: str):
        return getattr(self.wrapped_settings, name)


def test_generate_openclaw_config_uses_native_keyword_memory(monkeypatch, tmp_path):
    # Given
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    manager = AgentManager.__new__(AgentManager)
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="Memory Agent", creator_id=uuid.uuid4(), primary_model_id=None)

    # When
    config: dict[str, Any] = manager._generate_openclaw_config(agent, model=None)

    # Then
    assert config["memory"]["search"] == {"enabled": True, "provider": "none"}
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["plugins"] == {"slots": {"memory": "memory-core"}}


def test_generate_openclaw_config_maps_grok_subscription_to_xai_plugin(monkeypatch):
    settings = GuestSettings(agent_manager_module.settings)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    manager = AgentManager.__new__(AgentManager)
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="Grok Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    primary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="grok",
        model="grok-4.8",
        api_key_encrypted="xai-sub-access-token",
        auth_kind="grok_subscription",
        label="Grok SuperGrok",
    )
    secondary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key_encrypted="sk-ant-test",
        auth_kind="api_key",
        label="Claude",
    )
    fallback: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="xai",
        model="grok-4.5",
        api_key_encrypted="xai-fallback-token",
        auth_kind="api_key",
        label="Grok fallback",
    )

    config: dict[str, Any] = manager._generate_openclaw_config(
        agent, primary, secondary=secondary, fallback=fallback, selected=primary
    )

    assert "agent" not in config
    assert config["agents"]["defaults"]["model"]["primary"] == "xai/grok-4.8"
    assert config["agents"]["defaults"]["model"]["fallbacks"] == ["xai/grok-4.5"]
    assert config["agents"]["defaults"]["models"]["xai/grok-4.8"] == {"alias": "primary"}
    assert config["env"]["vars"]["XAI_API_KEY"] == "xai-fallback-token"
    assert "GROK_API_KEY" not in config["env"]["vars"]
    assert config["auth"]["profiles"][XAI_OAUTH_PROFILE_ID] == {"provider": "xai", "mode": "oauth"}
    assert config["auth"]["order"]["xai"] == [XAI_OAUTH_PROFILE_ID]
    assert config["hooks"]["enabled"] is True
    assert config["gateway"]["mode"] == "local"
    assert config["gateway"]["bind"] == "lan"
    assert config["agents"]["defaults"]["heartbeat"]["every"] == "30s"
    assert config["memory"]["search"] == {"enabled": True, "provider": "none"}
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["env"]["vars"]["MARACLAW_API_BASE"] == "http://maraclaw-engine:8000"


def test_generate_openclaw_config_omits_invalid_xai_console_key(monkeypatch):
    settings = GuestSettings(agent_manager_module.settings)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    manager = AgentManager.__new__(AgentManager)
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="Grok Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    primary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="grok",
        model="grok-4.8",
        api_key_encrypted="hgklshglsjgh-not-a-key",
        auth_kind="api_key",
        label="Grok 4.6",
    )

    config: dict[str, Any] = manager._generate_openclaw_config(agent, primary, selected=primary)

    assert "XAI_API_KEY" not in config["env"]["vars"]
    assert "auth" not in config


def test_write_guest_config_refreshes_native_memory_with_empty_process_cache(monkeypatch, tmp_path) -> None:
    # Given
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "_guest_config_fingerprints", {})
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid.uuid4(), name="Memory Agent", creator_id=uuid.uuid4())
    agent_dir = tmp_path / str(agent.id)
    agent_dir.mkdir()
    config_path = agent_dir / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "memory": {"search": {"enabled": False, "provider": "auto"}},
                "plugins": {
                    "slots": {"memory": "none"},
                    "entries": {"custom": {"enabled": True, "config": {"keep": "custom-value"}}},
                },
            }
        ),
        encoding="utf-8",
    )

    # When
    path = manager.write_guest_config(agent, primary=None)

    # Then
    assert path == config_path
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["memory"]["search"] == {"enabled": True, "provider": "none"}
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["plugins"] == {
        "slots": {"memory": "memory-core"},
        "entries": {"custom": {"enabled": True, "config": {"keep": "custom-value"}}},
    }


def test_write_guest_config_stores_xai_oauth_profile(monkeypatch, tmp_path) -> None:
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    monkeypatch.setattr(agent_manager_module, "decrypt_data", lambda raw, _key: raw)
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, name="Grok Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    expires = datetime.now(UTC) + timedelta(hours=2)
    primary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="grok",
        model="grok-4.8",
        api_key_encrypted="xai-sub-access-token",
        refresh_token_encrypted="xai-sub-refresh-token",
        token_expires_at=expires,
        auth_kind="grok_subscription",
        label="Grok SuperGrok",
    )

    path = manager.write_guest_config(agent, primary=primary, selected=primary)

    assert path is not None
    assert path == agent_dir / "openclaw.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert "XAI_API_KEY" not in config["env"]["vars"]
    assert config["auth"]["profiles"][XAI_OAUTH_PROFILE_ID]["mode"] == "oauth"
    profiles = json.loads((agent_dir / "agents" / "main" / "agent" / "auth-profiles.json").read_text(encoding="utf-8"))
    cred = profiles["profiles"][XAI_OAUTH_PROFILE_ID]
    assert cred["type"] == "oauth"
    assert cred["provider"] == "xai"
    assert cred["access"] == "xai-sub-access-token"
    assert cred["refresh"] == "xai-sub-refresh-token"
    assert cred["expires"] == int(expires.timestamp() * 1000)
    legacy = json.loads((agent_dir / "credentials" / "oauth.json").read_text(encoding="utf-8"))
    assert legacy["xai"]["access"] == "xai-sub-access-token"


def test_generate_openclaw_config_maps_chatgpt_subscription_to_openai_oauth(monkeypatch):
    settings = GuestSettings(agent_manager_module.settings)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    manager = AgentManager.__new__(AgentManager)
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="ChatGPT Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    primary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        model="gpt-5.4",
        api_key_encrypted="sk-not-used",
        auth_kind="chatgpt_subscription",
        label="ChatGPT",
    )
    fallback: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        model="gpt-6.1-sol",
        api_key_encrypted="sk-openai-key",
        auth_kind="api_key",
        label="GPT API",
    )

    config: dict[str, Any] = manager._generate_openclaw_config(agent, primary, fallback=fallback, selected=primary)

    assert config["agents"]["defaults"]["model"]["primary"] == "openai/gpt-5.4"
    assert config["env"]["vars"]["OPENAI_API_KEY"] == "sk-openai-key"
    assert config["auth"]["profiles"][OPENAI_OAUTH_PROFILE_ID] == {"provider": "openai", "mode": "oauth"}
    assert config["auth"]["order"]["openai"] == [OPENAI_OAUTH_PROFILE_ID]


def test_write_guest_config_stores_openai_oauth_profile(monkeypatch, tmp_path) -> None:
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    monkeypatch.setattr(agent_manager_module, "decrypt_data", lambda raw, _key: raw)
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    agent: Any = SimpleNamespace(id=agent_id, name="ChatGPT Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    expires = datetime.now(UTC) + timedelta(hours=2)
    access = "eyJhbGciOiJub25lIn0.eyJjaGF0Z3B0X2FjY291bnRfaWQiOiJhY2N0LWxpdmUifQ.sig"
    primary: Any = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        model="gpt-5.4",
        api_key_encrypted=access,
        refresh_token_encrypted="oa-sub-refresh-token",
        token_expires_at=expires,
        auth_kind="chatgpt_subscription",
        label="ChatGPT",
        oauth_account_id="acct-from-row",
    )

    path = manager.write_guest_config(agent, primary=primary, selected=primary)

    assert path is not None
    assert path == agent_dir / "openclaw.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert "OPENAI_API_KEY" not in config["env"]["vars"]
    assert config["auth"]["profiles"][OPENAI_OAUTH_PROFILE_ID]["mode"] == "oauth"
    profiles = json.loads((agent_dir / "agents" / "main" / "agent" / "auth-profiles.json").read_text(encoding="utf-8"))
    cred = profiles["profiles"][OPENAI_OAUTH_PROFILE_ID]
    assert cred["type"] == "oauth"
    assert cred["provider"] == "openai"
    assert cred["access"] == access
    assert cred["refresh"] == "oa-sub-refresh-token"
    assert cred["accountId"] == "acct-from-row"
    assert cred["expires"] == int(expires.timestamp() * 1000)
    legacy = json.loads((agent_dir / "credentials" / "oauth.json").read_text(encoding="utf-8"))
    assert legacy["openai"]["access"] == access
    assert legacy["openai"]["accountId"] == "acct-from-row"


async def test_start_container_writes_openclaw_config_at_state_root_and_passes_env(monkeypatch, tmp_path):
    # Given
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient(
        inspect_result=SimpleNamespace(
            id="stale-container",
            name=f"maraclaw-agent-{str(agent_id)[:8]}",
            config=SimpleNamespace(labels={"maraclaw.agent_id": str(agent_id)}),
            host_config=SimpleNamespace(port_bindings={}),
        )
    )
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(requested_agent_id):
        assert requested_agent_id == agent_id
        return agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)
    agent: Any = SimpleNamespace(
        id=agent_id,
        name="Memory Agent",
        creator_id=uuid.uuid4(),
        primary_model_id=None,
        gogcli_enabled=False,
        setup_pending=False,
    )

    # When
    container_id = await manager.replace_container(agent=agent)

    # Then
    assert container_id == "container-1234567890"
    host_config_path = agent_dir / "openclaw.json"
    assert host_config_path.exists()
    assert not (agent_dir / ".openclaw" / "openclaw.json").exists()
    config = json.loads(host_config_path.read_text(encoding="utf-8"))
    assert config["plugins"] == {"slots": {"memory": "memory-core"}}
    assert (agent_dir / "workspace" / "skills" / "maraclaw-sync" / "SKILL.md").is_file()

    run_kwargs = docker_client.run_kwargs
    assert run_kwargs is not None
    assert docker_client.run_args == (
        settings.MARACLAW_OPENCLAW_IMAGE,
        [
            "-s",
            "--",
            "/usr/local/bin/bootstrap-officecli.sh",
            "/usr/local/bin/validate-gogcli.sh",
            "openclaw",
            "gateway",
        ],
    )
    assert run_kwargs["entrypoint"] == "tini"
    assert docker_client.container.stop_calls
    assert docker_client.container.remove_calls
    assert run_kwargs["detach"] is True
    assert run_kwargs["name"] == f"maraclaw-agent-{agent.id}"
    assert run_kwargs["volumes"] == [(str(agent_dir), "/home/node/.openclaw", "rw")]
    assert run_kwargs["envs"]["OPENCLAW_HOME"] == "/home/node"
    assert run_kwargs["envs"]["OPENCLAW_STATE_DIR"] == "/home/node/.openclaw"
    assert run_kwargs["envs"]["OPENCLAW_CONFIG_PATH"] == "/home/node/.openclaw/openclaw.json"
    assert "OPENCLAW_GATEWAY_TOKEN" in run_kwargs["envs"]
    assert run_kwargs["envs"]["MARACLAW_API_BASE"] == "http://maraclaw-engine:8000"
    assert run_kwargs["networks"] == [settings.DOCKER_NETWORK]
    assert run_kwargs["publish"] == [(f"127.0.0.1:{agent.container_port}", settings.OPENCLAW_GATEWAY_PORT)]
    assert run_kwargs["restart"] == "unless-stopped"
    assert run_kwargs["labels"] == {
        "maraclaw.agent_id": str(agent.id),
        "maraclaw.agent_name": agent.name,
    }


async def test_start_container_refreshes_native_memory_config(monkeypatch, tmp_path):
    # Given
    settings = GuestSettings(agent_manager_module.settings, storage_root=str(tmp_path))
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    (agent_dir / "openclaw.json").write_text(
        json.dumps({"memory": {"search": {"enabled": False, "provider": "auto"}}}),
        encoding="utf-8",
    )
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient()
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(requested_agent_id):
        assert requested_agent_id == agent_id
        return agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)
    agent: Any = SimpleNamespace(
        id=agent_id,
        name="Memory Agent",
        creator_id=uuid.uuid4(),
        primary_model_id=None,
        gogcli_enabled=False,
        setup_pending=False,
    )

    # When
    container_id = await manager.replace_container(agent=agent)

    # Then
    assert container_id == "container-1234567890"
    config = json.loads((agent_dir / "openclaw.json").read_text(encoding="utf-8"))
    assert config["memory"]["search"] == {"enabled": True, "provider": "none"}
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["plugins"] == {"slots": {"memory": "memory-core"}}


async def test_start_container_returns_none_when_pywhales_client_is_missing(monkeypatch, tmp_path):
    # Given
    class FakeClientNotFoundError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "ClientNotFoundError", FakeClientNotFoundError, raising=False)
    settings = GuestSettings(agent_manager_module.settings)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient(run_error=FakeClientNotFoundError("docker cli missing"))
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(requested_agent_id):
        assert requested_agent_id == agent_id
        return agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)
    agent: Any = SimpleNamespace(
        id=agent_id,
        name="Missing Docker Agent",
        creator_id=uuid.uuid4(),
        primary_model_id=None,
        gogcli_enabled=False,
        setup_pending=False,
    )

    # When
    container_id = await manager.replace_container(agent=agent)

    # Then
    assert container_id is None
    assert agent.status == "error"
    assert docker_client.run_args is not None
    assert docker_client.run_args[0] == settings.MARACLAW_OPENCLAW_IMAGE


async def test_stop_container_clears_status_and_container_id_when_container_missing(monkeypatch):
    # Given
    class FakeNoSuchContainerError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "NoSuchContainer", FakeNoSuchContainerError, raising=False)
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    docker_client = RecordingDockerClient(
        inspect_result=SimpleNamespace(
            id="missing-container",
            name="legacy-name",
            config=SimpleNamespace(labels={"maraclaw.agent_id": str(agent_id)}),
            host_config=SimpleNamespace(port_bindings={}),
        ),
        stop_error=FakeNoSuchContainerError("missing"),
    )
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent: Any = SimpleNamespace(id=agent_id, name="Missing Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "missing-container"
    agent.status = AgentStatus.RUNNING

    # When
    stopped = await manager.stop_container(agent)

    # Then
    assert stopped is True
    assert agent.status == "stopped"
    assert agent.container_id is None
    assert docker_client.container.stop_calls == [("missing-container", {"time": 10})]


async def test_remove_container_clears_only_container_id_when_container_missing(monkeypatch):
    # Given
    class FakeNoSuchContainerError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "NoSuchContainer", FakeNoSuchContainerError, raising=False)
    manager = AgentManager.__new__(AgentManager)
    agent_id = uuid.uuid4()
    docker_client = RecordingDockerClient(
        inspect_result=SimpleNamespace(
            id="missing-container",
            name="legacy-name",
            config=SimpleNamespace(labels={"maraclaw.agent_id": str(agent_id)}),
            host_config=SimpleNamespace(port_bindings={}),
        ),
        stop_error=FakeNoSuchContainerError("missing"),
    )
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent: Any = SimpleNamespace(id=agent_id, name="Missing Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "missing-container"
    agent.container_port = 19876
    agent.status = AgentStatus.RUNNING

    # When
    removed = await manager.remove_container(agent)

    # Then
    assert removed is True
    assert agent.container_id is None
    assert agent.container_port == 19876
    assert docker_client.container.stop_calls == [("missing-container", {"time": 10})]


async def test_stop_and_remove_container_use_pywhales_api_stop_time_and_remove(monkeypatch):
    # Given
    manager = AgentManager.__new__(AgentManager)
    agent = AgentRecord(id=uuid.uuid4(), name="Running Agent", creator_id=uuid.uuid4())
    docker_client = RecordingDockerClient(
        inspect_result=SimpleNamespace(
            id="running-container",
            name="legacy-name",
            config=SimpleNamespace(labels={"maraclaw.agent_id": str(agent.id)}),
            host_config=SimpleNamespace(port_bindings={}),
        )
    )
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent.container_id = "running-container"
    agent.container_port = 19876
    agent.status = AgentStatus.RUNNING

    # When
    stopped = await manager.stop_container(agent)
    agent.container_id = "running-container"
    removed = await manager.remove_container(agent)

    # Then
    assert stopped is True
    assert removed is True
    assert docker_client.container.stop_calls == [
        ("running-container", {"time": 10}),
        ("running-container", {"time": 10}),
    ]
    assert docker_client.container.remove_calls == [["running-container"]]
    assert agent.status == "stopped"
    assert agent.container_id is None
    assert agent.container_port is None


def test_get_container_status_reads_pywhales_fields(monkeypatch):
    # Given
    manager = AgentManager.__new__(AgentManager)
    inspected_container = SimpleNamespace(
        state=SimpleNamespace(running=True, status="running"),
        network_settings=SimpleNamespace(ports={"18789/tcp": [{"HostPort": "19876"}]}),
        created="2026-01-02T03:04:05Z",
    )
    docker_client = RecordingDockerClient(inspect_result=inspected_container)
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="Status Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "status-container"
    agent.status = "running"

    # When
    status = manager.get_container_status(agent)

    # Then
    assert status == {
        "running": True,
        "status": "running",
        "ports": {"18789/tcp": [{"HostPort": "19876"}]},
        "created": "2026-01-02T03:04:05Z",
    }
    assert docker_client.container.inspect_calls == ["status-container"]


def test_get_container_status_returns_not_found_when_pywhales_inspect_misses(monkeypatch):
    # Given
    class FakeNoSuchContainerError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "NoSuchContainer", FakeNoSuchContainerError, raising=False)
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient(inspect_error=FakeNoSuchContainerError("missing"))
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent: Any = SimpleNamespace(id=uuid.uuid4(), name="Missing Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "missing-container"
    agent.status = "running"

    # When
    status = manager.get_container_status(agent)

    # Then
    assert status == {"running": False, "status": "not_found"}
    assert docker_client.container.inspect_calls == ["missing-container"]


async def test_lifecycle_methods_treat_missing_pywhales_client_as_docker_error(monkeypatch):
    # Given
    class FakeClientNotFoundError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "ClientNotFoundError", FakeClientNotFoundError, raising=False)
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient(
        inspect_error=FakeClientNotFoundError("docker cli missing"),
        stop_error=FakeClientNotFoundError("docker cli missing"),
    )
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(), name="Missing Docker Agent", creator_id=uuid.uuid4(), primary_model_id=None
    )
    agent.container_id = "container-without-cli"
    agent.container_port = 19876
    agent.status = "running"

    # When
    stopped = await manager.stop_container(agent)
    removed = await manager.remove_container(agent)
    status = manager.get_container_status(agent)

    # Then
    assert stopped is False
    assert removed is False
    assert status == {"running": False, "status": "error"}
    assert agent.container_id == "container-without-cli"
    assert agent.container_port == 19876
