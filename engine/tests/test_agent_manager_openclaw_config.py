import json
import uuid
from types import SimpleNamespace

from app.config import Settings
from app.services import agent_manager as agent_manager_module
from app.services.agent_manager import AgentManager


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


class OpenClawMemorySettings:
    def __init__(self, wrapped_settings, memory_enabled: bool, plugin_version: str = "1.0.1"):
        self.wrapped_settings = wrapped_settings
        self.OPENCLAW_MEMORY_TENCENTDB_ENABLED = memory_enabled
        self.TENCENTDB_PLUGIN_VERSION = plugin_version

    def __getattr__(self, name: str):
        return getattr(self.wrapped_settings, name)


def test_openclaw_tencentdb_memory_setting_defaults_to_enabled():
    assert Settings.model_fields["OPENCLAW_MEMORY_TENCENTDB_ENABLED"].default is True


def test_tencentdb_plugin_version_setting_defaults_to_pinned_version():
    assert Settings.model_fields["TENCENTDB_PLUGIN_VERSION"].default == "1.0.1"


def test_generate_openclaw_config_includes_tencentdb_memory_plugin_when_enabled(monkeypatch):
    # Given
    settings = OpenClawMemorySettings(agent_manager_module.settings, memory_enabled=True)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Memory Agent", creator_id=uuid.uuid4(), primary_model_id=None)

    # When
    config = manager._generate_openclaw_config(agent, model=None)

    # Then
    assert config["plugins"]["enabled"] is True
    assert config["plugins"]["slots"]["memory"] == "memory-tencentdb"
    assert config["plugins"]["slots"]["contextEngine"] == "memory-tencentdb"
    assert config["plugins"]["entries"]["memory-tencentdb"]["enabled"] is True
    assert config["plugins"]["entries"]["memory-tencentdb"]["hooks"]["allowConversationAccess"] is True
    assert config["plugins"]["entries"]["memory-tencentdb"]["config"]["storeBackend"] == "sqlite"
    assert config["plugins"]["entries"]["memory-tencentdb"]["config"]["offload"] == {"enabled": True}


def test_generate_openclaw_config_maps_grok_subscription_to_xai_plugin(monkeypatch):
    settings = OpenClawMemorySettings(agent_manager_module.settings, memory_enabled=False)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_PROXY_ENABLED", False)
    monkeypatch.setattr(agent_manager_module.settings, "LINKUP_API_KEY", "")
    monkeypatch.setattr(agent_manager_module, "get_model_api_key", lambda row: row.api_key_encrypted)
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Grok Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    primary = SimpleNamespace(
        id=uuid.uuid4(),
        provider="grok",
        model="grok-4.6",
        api_key_encrypted="xai-sub-access-token",
        auth_kind="grok_subscription",
        label="Grok SuperGrok",
    )
    secondary = SimpleNamespace(
        id=uuid.uuid4(),
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key_encrypted="sk-ant-test",
        auth_kind="api_key",
        label="Claude",
    )
    fallback = SimpleNamespace(
        id=uuid.uuid4(),
        provider="xai",
        model="grok-4.5",
        api_key_encrypted="xai-fallback-token",
        auth_kind="api_key",
        label="Grok fallback",
    )

    config = manager._generate_openclaw_config(
        agent, primary, secondary=secondary, fallback=fallback, selected=primary
    )

    assert "agent" not in config
    assert config["agents"]["defaults"]["model"]["primary"] == "xai/grok-4.6"
    assert config["agents"]["defaults"]["model"]["fallbacks"] == ["xai/grok-4.5"]
    assert config["agents"]["defaults"]["models"]["xai/grok-4.6"] == {"alias": "primary"}
    assert config["env"]["vars"]["XAI_API_KEY"] == "xai-sub-access-token"
    assert "GROK_API_KEY" not in config["env"]["vars"]
    assert config["hooks"]["enabled"] is True
    assert config["gateway"]["mode"] == "local"
    assert config["gateway"]["bind"] == "loopback"
    assert config["agents"]["defaults"]["heartbeat"]["every"] == "1m"
    assert config["env"]["vars"]["MARACLAW_API_BASE"] == "http://maraclaw-engine:8000"


def test_generate_openclaw_config_omits_plugins_when_tencentdb_memory_disabled(monkeypatch):
    # Given
    settings = OpenClawMemorySettings(agent_manager_module.settings, memory_enabled=True)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(agent_manager_module.settings, "OPENCLAW_MEMORY_TENCENTDB_ENABLED", False)
    manager = AgentManager.__new__(AgentManager)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Memory Agent", creator_id=uuid.uuid4(), primary_model_id=None)

    # When
    config = manager._generate_openclaw_config(agent, model=None)

    # Then
    assert "plugins" not in config


async def test_start_container_writes_openclaw_config_at_state_root_and_passes_env(monkeypatch, tmp_path):
    # Given
    settings = OpenClawMemorySettings(agent_manager_module.settings, memory_enabled=True, plugin_version="7.8.9")
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient()
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(requested_agent_id):
        assert requested_agent_id == agent_id
        return agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)
    agent = SimpleNamespace(id=agent_id, name="Memory Agent", creator_id=uuid.uuid4(), primary_model_id=None)

    # When
    container_id = await manager.start_container(db=SimpleNamespace(), agent=agent)

    # Then
    assert container_id == "container-1234567890"
    host_config_path = agent_dir / "openclaw.json"
    assert host_config_path.exists()
    assert not (agent_dir / ".openclaw" / "openclaw.json").exists()
    config = json.loads(host_config_path.read_text(encoding="utf-8"))
    assert config["plugins"]["slots"]["memory"] == "memory-tencentdb"
    assert config["plugins"]["slots"]["contextEngine"] == "memory-tencentdb"
    assert config["plugins"]["entries"]["memory-tencentdb"]["config"]["offload"] == {"enabled": True}
    assert (agent_dir / "workspace" / "skills" / "maraclaw-sync" / "SKILL.md").is_file()

    run_kwargs = docker_client.run_kwargs
    assert run_kwargs is not None
    assert docker_client.run_args == (settings.OPENCLAW_IMAGE,)
    assert run_kwargs["detach"] is True
    assert run_kwargs["name"] == f"maraclaw-agent-{str(agent.id)[:8]}"
    assert run_kwargs["volumes"] == [(str(agent_dir), "/home/node/.openclaw", "rw")]
    assert run_kwargs["envs"]["OPENCLAW_HOME"] == "/home/node"
    assert run_kwargs["envs"]["OPENCLAW_STATE_DIR"] == "/home/node/.openclaw"
    assert run_kwargs["envs"]["OPENCLAW_CONFIG_PATH"] == "/home/node/.openclaw/openclaw.json"
    assert run_kwargs["envs"]["TENCENTDB_PLUGIN_VERSION"] == "7.8.9"
    assert "OPENCLAW_GATEWAY_TOKEN" in run_kwargs["envs"]
    assert run_kwargs["envs"]["MARACLAW_API_BASE"] == "http://maraclaw-engine:8000"
    assert run_kwargs["networks"] == [settings.DOCKER_NETWORK]
    assert run_kwargs["publish"] == [(agent.container_port, settings.OPENCLAW_GATEWAY_PORT)]
    assert run_kwargs["restart"] == "unless-stopped"
    assert run_kwargs["labels"] == {
        "maraclaw.agent_id": str(agent.id),
        "maraclaw.agent_name": agent.name,
    }


async def test_start_container_returns_none_when_python_on_whales_client_is_missing(monkeypatch, tmp_path):
    # Given
    class FakeClientNotFoundError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "ClientNotFoundError", FakeClientNotFoundError, raising=False)
    settings = OpenClawMemorySettings(agent_manager_module.settings, memory_enabled=True)
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
    agent = SimpleNamespace(id=agent_id, name="Missing Docker Agent", creator_id=uuid.uuid4(), primary_model_id=None)

    # When
    container_id = await manager.start_container(db=SimpleNamespace(), agent=agent)

    # Then
    assert container_id is None
    assert agent.status == "error"
    assert docker_client.run_args == (settings.OPENCLAW_IMAGE,)


async def test_stop_container_clears_status_and_container_id_when_container_missing(monkeypatch):
    # Given
    class FakeNoSuchContainerError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "NoSuchContainer", FakeNoSuchContainerError, raising=False)
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient(stop_error=FakeNoSuchContainerError("missing"))
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Missing Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "missing-container"
    agent.status = "running"

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
    docker_client = RecordingDockerClient(stop_error=FakeNoSuchContainerError("missing"))
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Missing Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "missing-container"
    agent.container_port = 19876
    agent.status = "running"

    # When
    removed = await manager.remove_container(agent)

    # Then
    assert removed is True
    assert agent.container_id is None
    assert agent.container_port == 19876
    assert docker_client.container.stop_calls == [("missing-container", {"time": 10})]


async def test_stop_and_remove_container_use_python_on_whales_api_stop_time_and_remove():
    # Given
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient()
    manager.docker_client = docker_client
    agent = SimpleNamespace(id=uuid.uuid4(), name="Running Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "running-container"
    agent.container_port = 19876
    agent.status = "running"

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


def test_get_container_status_reads_python_on_whales_fields(monkeypatch):
    # Given
    manager = AgentManager.__new__(AgentManager)
    inspected_container = SimpleNamespace(
        state=SimpleNamespace(running=True, status="running"),
        network_settings=SimpleNamespace(ports={"18789/tcp": [{"HostPort": "19876"}]}),
        created="2026-01-02T03:04:05Z",
    )
    docker_client = RecordingDockerClient(inspect_result=inspected_container)
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Status Agent", creator_id=uuid.uuid4(), primary_model_id=None)
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


def test_get_container_status_returns_not_found_when_python_on_whales_inspect_misses(monkeypatch):
    # Given
    class FakeNoSuchContainerError(Exception):
        pass

    monkeypatch.setattr(agent_manager_module, "NoSuchContainer", FakeNoSuchContainerError, raising=False)
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient(inspect_error=FakeNoSuchContainerError("missing"))
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)
    agent = SimpleNamespace(id=uuid.uuid4(), name="Missing Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "missing-container"
    agent.status = "running"

    # When
    status = manager.get_container_status(agent)

    # Then
    assert status == {"running": False, "status": "not_found"}
    assert docker_client.container.inspect_calls == ["missing-container"]


async def test_lifecycle_methods_treat_missing_python_on_whales_client_as_docker_error(monkeypatch):
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
    agent = SimpleNamespace(
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
