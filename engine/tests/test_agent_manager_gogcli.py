import uuid
from types import SimpleNamespace
from typing import override

from python_on_whales.exceptions import DockerException

from app.services import agent_manager as agent_manager_module, gogcli_oauth, gogcli_runtime
from app.services.agent_manager import AgentManager


class RecordingDockerClient:
    def __init__(self) -> None:
        self.run_args = None
        self.run_kwargs = None
        self.execute_calls = []
        self.execute_output = "Open https://accounts.google.com/o/oauth2/v2/auth?client_id=public-client to continue"

    def run(self, *args, **kwargs):
        self.run_args = args
        self.run_kwargs = kwargs
        return SimpleNamespace(id="container-1234567890")

    def execute(self, container_id, command, *, stream=False):
        self.execute_calls.append((container_id, command, stream))
        return self.execute_output


class NonZeroDockerClient(RecordingDockerClient):
    @override
    def execute(self, container_id, command, *, stream=False):
        self.execute_calls.append((container_id, command, stream))
        raise DockerException(["docker", "exec"], 1, b"not logged in", b"raw-gog-stderr-should-not-leak")


def assert_secret_wrapped_gog_command(execute_call, expected_container_id, expected_gog_args) -> None:
    container_id, command, stream = execute_call
    assert container_id == expected_container_id
    assert stream is False
    assert command[0] == "sh"
    assert command[1] == "-c"
    assert command[3] == "gogcli-secret-env"
    assert command[4:] == expected_gog_args
    script = command[2]
    assert 'GOG_KEYRING_PASSWORD=$(cat "$GOG_KEYRING_PASSWORD_FILE")' in script
    assert "export GOG_KEYRING_PASSWORD" in script
    assert 'exec "$@"' in script
    assert "correct horse battery staple" not in " ".join(command)


class GogcliSettings:
    def __init__(self, wrapped_settings, *, enabled: bool) -> None:
        self.wrapped_settings = wrapped_settings
        self.GOGCLI_ENABLED = enabled

    def __getattr__(self, name: str):
        return getattr(self.wrapped_settings, name)


def _enable_gogcli(monkeypatch, tmp_path, *, enabled: bool) -> None:
    settings = GogcliSettings(agent_manager_module.settings, enabled=enabled)
    monkeypatch.setattr(agent_manager_module, "settings", settings)
    monkeypatch.setattr(gogcli_runtime, "GOGCLI_ENABLED", enabled)
    monkeypatch.setattr(gogcli_runtime.settings, "GOGCLI_ENABLED", enabled, raising=False)
    monkeypatch.setattr(gogcli_runtime.settings, "STORAGE_LOCAL_ROOT", str(tmp_path / "local-root"))
    monkeypatch.setattr(gogcli_runtime.settings, "AGENT_DATA_DIR", str(tmp_path / "agent-data"))


async def test_start_container_adds_gogcli_secret_mount_when_secret_exists(monkeypatch, tmp_path) -> None:
    # Given
    _enable_gogcli(monkeypatch, tmp_path, enabled=True)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    gogcli_runtime.write_gogcli_keyring_secret(agent_id, "correct horse battery staple")
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient()
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(requested_agent_id):
        assert requested_agent_id == agent_id
        return agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)
    restore_calls = []

    async def restore_state(_db, requested_agent_id, restored_agent_dir):
        restore_calls.append(
            (requested_agent_id, restored_agent_dir, gogcli_runtime.gogcli_secret_file(agent_id).exists())
        )

    monkeypatch.setattr(agent_manager_module, "restore_gogcli_state", restore_state)
    agent = SimpleNamespace(id=agent_id, name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.gogcli_enabled = True

    # When
    await manager.start_container(db=SimpleNamespace(), agent=agent)

    # Then
    assert docker_client.run_kwargs is not None
    envs = docker_client.run_kwargs["envs"]
    assert envs["GOG_HOME"] == "/home/node/.openclaw/gogcli"
    assert envs["GOG_KEYRING_BACKEND"] == "file"
    assert envs["GOG_KEYRING_PASSWORD_FILE"] == "/run/secrets/gogcli_keyring_password"
    assert "GOG_KEYRING_PASSWORD" not in envs
    assert (
        str(gogcli_runtime.gogcli_secret_file(agent_id)),
        "/run/secrets/gogcli_keyring_password",
        "ro",
    ) in docker_client.run_kwargs["volumes"]
    assert (str(agent_dir), "/home/node/.openclaw", "rw") in docker_client.run_kwargs["volumes"]
    assert restore_calls == [(agent_id, agent_dir, True)]


async def test_start_container_restores_gogcli_state_before_docker_extras(monkeypatch, tmp_path) -> None:
    # Given
    _enable_gogcli(monkeypatch, tmp_path, enabled=True)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient()
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(_requested_agent_id):
        return agent_dir

    async def restore_state(_db, requested_agent_id, restored_agent_dir):
        assert requested_agent_id == agent_id
        gogcli_runtime.write_gogcli_keyring_secret(requested_agent_id, "restored password")
        assert restored_agent_dir == agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)
    monkeypatch.setattr(agent_manager_module, "restore_gogcli_state", restore_state)
    agent = SimpleNamespace(id=agent_id, name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.gogcli_enabled = True

    # When
    await manager.start_container(db=SimpleNamespace(), agent=agent)

    # Then
    assert docker_client.run_kwargs is not None
    assert docker_client.run_kwargs["envs"]["GOG_KEYRING_PASSWORD_FILE"] == "/run/secrets/gogcli_keyring_password"
    assert (
        str(gogcli_runtime.gogcli_secret_file(agent_id)),
        "/run/secrets/gogcli_keyring_password",
        "ro",
    ) in docker_client.run_kwargs["volumes"]


async def test_start_container_omits_gogcli_env_when_agent_flag_is_false(monkeypatch, tmp_path) -> None:
    # Given
    _enable_gogcli(monkeypatch, tmp_path, enabled=True)
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / "agent-workspace"
    agent_dir.mkdir()
    manager = AgentManager.__new__(AgentManager)
    docker_client = RecordingDockerClient()
    monkeypatch.setattr(manager, "docker_client", docker_client, raising=False)

    async def materialize_agent_dir(requested_agent_id):
        assert requested_agent_id == agent_id
        return agent_dir

    monkeypatch.setattr(manager, "_materialize_agent_dir", materialize_agent_dir)

    async def restore_state(_db, _requested_agent_id, _restored_agent_dir):
        msg = "disabled gogcli agents must not restore gogcli credential state"
        raise AssertionError(msg)

    monkeypatch.setattr(agent_manager_module, "restore_gogcli_state", restore_state)
    agent = SimpleNamespace(id=agent_id, name="Plain Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.gogcli_enabled = False

    # When
    await manager.start_container(db=SimpleNamespace(), agent=agent)

    # Then
    assert docker_client.run_kwargs is not None
    envs = docker_client.run_kwargs["envs"]
    assert "GOG_HOME" not in envs
    assert "GOG_KEYRING_BACKEND" not in envs
    assert "GOG_KEYRING_PASSWORD_FILE" not in envs
    assert "GOG_KEYRING_PASSWORD" not in envs
    assert all(
        container_path != "/run/secrets/gogcli_keyring_password"
        for _, container_path, _ in docker_client.run_kwargs["volumes"]
    )


async def test_start_gogcli_auth_executes_gogcli_with_argv_sequence() -> None:
    # Given
    docker_client = RecordingDockerClient()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "container-oauth"

    # When
    result = await gogcli_oauth.start_gogcli_auth(docker_client, agent, "maraclaw@example.com")

    # Then
    assert result.auth_url == "https://accounts.google.com/o/oauth2/v2/auth?client_id=public-client"
    assert len(docker_client.execute_calls) == 1
    assert_secret_wrapped_gog_command(
        docker_client.execute_calls[0],
        "container-oauth",
        [
            "gog",
            "auth",
            "add",
            "maraclaw@example.com",
            "--services",
            "all-user",
            "--remote",
            "--step",
            "1",
            "--plain",
            "--no-input",
        ],
    )


async def test_start_gogcli_auth_fails_safely_without_docker_client() -> None:
    # Given
    agent = SimpleNamespace(id=uuid.uuid4(), name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "container-oauth"

    # When
    result = await gogcli_oauth.start_gogcli_auth(None, agent, "maraclaw@example.com")

    # Then
    assert result.started is False
    assert result.auth_url is None
    assert result.detail == "Agent container is not available"


async def test_start_gogcli_auth_fails_safely_without_container_id() -> None:
    # Given
    docker_client = RecordingDockerClient()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = None

    # When
    result = await gogcli_oauth.start_gogcli_auth(docker_client, agent, "maraclaw@example.com")

    # Then
    assert result.started is False
    assert result.auth_url is None
    assert result.detail == "Agent container is not running"
    assert docker_client.execute_calls == []


async def test_gogcli_auth_status_executes_status_with_argv_sequence() -> None:
    # Given
    docker_client = RecordingDockerClient()
    docker_client.execute_output = "Logged in as maraclaw@example.com"
    agent = SimpleNamespace(id=uuid.uuid4(), name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "container-oauth"

    # When
    status = await gogcli_oauth.get_gogcli_auth_status(docker_client, agent)

    # Then
    assert status.authenticated is True
    assert status.account_hint == "maraclaw@example.com"
    assert len(docker_client.execute_calls) == 1
    assert_secret_wrapped_gog_command(
        docker_client.execute_calls[0],
        "container-oauth",
        ["gog", "auth", "list", "--check", "--json", "--no-input"],
    )


async def test_gogcli_auth_status_parses_nonzero_exec_output_safely() -> None:
    # Given
    docker_client = NonZeroDockerClient()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Gog Agent", creator_id=uuid.uuid4(), primary_model_id=None)
    agent.container_id = "container-oauth"

    # When
    status = await gogcli_oauth.get_gogcli_auth_status(docker_client, agent)

    # Then
    assert status.authenticated is False
    assert status.account_hint is None
    assert status.detail == "Not authenticated"
    assert "raw-gog-stderr-should-not-leak" not in repr(status.model_dump())
