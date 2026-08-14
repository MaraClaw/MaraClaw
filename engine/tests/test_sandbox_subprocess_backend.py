import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.sandbox.base import resolve_exec_timeout
from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local.subprocess_backend import SubprocessBackend


class BlockingStream:
    async def read(self, _: int) -> bytes:
        return b""


class BlockingProcess:
    def __init__(self):
        self.pid = 999_999
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.stdout = BlockingStream()
        self.stderr = BlockingStream()
        self.killed = False
        self.reaped = False

    async def wait(self) -> int:
        self.started.set()
        await self.finished.wait()
        self.reaped = True
        return self.returncode or -9

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.finished.set()


@pytest.mark.asyncio
async def test_ensure_workspace_venv_requires_an_absolute_uv_executable(tmp_path: Path):
    # Given: no existing virtual environment and no trusted uv executable
    backend = SubprocessBackend(SandboxConfig())

    # When: the sandbox prepares its venv
    with (
        patch("app.services.sandbox.local.subprocess_backend.shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="uv executable"),
    ):
        # Then: it fails before launching a partial executable name
        await backend._ensure_workspace_venv(tmp_path / ".venv")


@pytest.mark.asyncio
async def test_ensure_workspace_venv_terminates_uv_process_group_when_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: uv is creating a venv and its child process is still running
    backend = SubprocessBackend(SandboxConfig())
    process = BlockingProcess()
    launch_sessions: list[bool] = []
    terminated: list[BlockingProcess] = []

    async def launch(*_: str, **kwargs: str | bool) -> BlockingProcess:
        launch_sessions.append(kwargs.get("start_new_session") is True)
        return process

    async def terminate(child: BlockingProcess) -> None:
        terminated.append(child)
        child.kill()
        await child.wait()

    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.shutil.which", lambda _: "/usr/bin/uv")
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.asyncio.create_subprocess_exec", launch)
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend._terminate_process_group", terminate)

    # When: cancellation interrupts uv while its process wait is pending
    setup = asyncio.create_task(backend._ensure_workspace_venv(tmp_path / ".venv"))
    await process.started.wait()
    setup.cancel()

    # Then: cleanup reaps the uv child before propagating cancellation
    with pytest.raises(asyncio.CancelledError):
        await setup
    assert launch_sessions == [True]
    assert terminated == [process]
    assert process.reaped


def test_build_bwrap_command_mounts_workspace_tmp_at_guest_tmp(tmp_path: Path):
    # Given: an isolated, no-network sandbox workspace
    backend = SubprocessBackend(SandboxConfig(allow_network=False))
    work_path = tmp_path / "workspace"
    venv_path = work_path / ".venv"
    (work_path / ".tmp").mkdir(parents=True)
    venv_path.mkdir()

    # When: the bubblewrap command is constructed
    with patch("app.services.sandbox.local.subprocess_backend.shutil.which", return_value="/usr/bin/bwrap"):
        command = backend._build_bwrap_command(["/workspace/.venv/bin/python", "script.py"], work_path, venv_path)

    # Then: guest /tmp is workspace-owned and network remains isolated
    assert command is not None
    guest_tmp = str(Path("/") / "tmp")
    assert ["--bind", str(work_path / ".tmp"), guest_tmp] == command[
        command.index("--bind", command.index(str(venv_path)) + 1) :
    ][:3]
    assert "--unshare-net" in command


@pytest.mark.asyncio
async def test_execute_terminates_child_when_cancelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Given: a launched sandbox child that does not complete by itself
    backend = SubprocessBackend(SandboxConfig())
    process = BlockingProcess()

    async def ensure_venv(_: Path) -> None:
        return None

    async def launch(*_: object, **__: object) -> BlockingProcess:
        return process

    monkeypatch.setattr(backend, "_ensure_workspace_venv", ensure_venv)
    monkeypatch.setattr(backend, "_build_bwrap_command", lambda *_: ["sandbox"])
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.asyncio.create_subprocess_exec", launch)

    # When: the caller cancels active execution
    execution = asyncio.create_task(backend.execute("print('x')", "python", work_dir=str(tmp_path)))
    await process.started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    # Then: the child is killed before cancellation reaches the caller
    assert process.killed
    assert not (tmp_path / "_exec_tmp.py").exists()


def test_subprocess_backend_proxy_env_propagation(tmp_path: Path) -> None:
    # Given: network allowed with explicit sandbox proxies
    config = SandboxConfig(
        allow_network=True,
        http_proxy="http://127.0.0.1:8080",
        https_proxy="http://127.0.0.1:8081",
        no_proxy="localhost,127.0.0.1",
    )
    backend = SubprocessBackend(config)

    # When
    env = backend._build_safe_env(tmp_path)

    # Then: both case variants are present
    assert env.get("http_proxy") == "http://127.0.0.1:8080"
    assert env.get("HTTP_PROXY") == "http://127.0.0.1:8080"
    assert env.get("https_proxy") == "http://127.0.0.1:8081"
    assert env.get("HTTPS_PROXY") == "http://127.0.0.1:8081"
    assert env.get("no_proxy") == "localhost,127.0.0.1"
    assert env.get("NO_PROXY") == "localhost,127.0.0.1"


def test_subprocess_backend_omits_proxy_when_network_disallowed(tmp_path: Path) -> None:
    # Given: proxies configured but network disabled
    config = SandboxConfig(
        allow_network=False,
        http_proxy="http://127.0.0.1:8080",
        https_proxy="http://127.0.0.1:8081",
        no_proxy="localhost",
    )
    backend = SubprocessBackend(config)

    # When
    env = backend._build_safe_env(tmp_path)

    # Then: secrets are not exposed to the guest
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        assert key not in env


def test_subprocess_backend_ignores_process_env_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: no config proxy, but process env has proxies
    monkeypatch.setenv("HTTP_PROXY", "http://evil.example:9")
    monkeypatch.setenv("http_proxy", "http://evil.example:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:9")
    monkeypatch.setenv("NO_PROXY", "localhost")
    backend = SubprocessBackend(SandboxConfig(allow_network=True))

    # When
    env = backend._build_safe_env(tmp_path)

    # Then: process env is not inherited into the sandbox
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        assert key not in env


def test_subprocess_backend_partial_proxy_only_sets_http(tmp_path: Path) -> None:
    config = SandboxConfig(allow_network=True, http_proxy="http://only-http:8080")
    env = SubprocessBackend(config)._build_safe_env(tmp_path)
    assert env["http_proxy"] == "http://only-http:8080"
    assert env["HTTP_PROXY"] == "http://only-http:8080"
    assert "https_proxy" not in env
    assert "no_proxy" not in env


def test_subprocess_backend_bwrap_isolation_flags_and_no_proxy_argv(tmp_path: Path) -> None:
    # Given: network disabled (expect --unshare-net) and proxies configured
    config = SandboxConfig(
        allow_network=False,
        http_proxy="http://proxy.example.com:8080",
        https_proxy="http://proxy.example.com:8443",
        no_proxy="localhost",
    )
    backend = SubprocessBackend(config)
    work_path = tmp_path / "workspace"
    venv_path = work_path / ".venv"
    (work_path / ".tmp").mkdir(parents=True)
    venv_path.mkdir()

    # When
    with patch("app.services.sandbox.local.subprocess_backend.shutil.which", return_value="/usr/bin/bwrap"):
        cmd = backend._build_bwrap_command(["python3", "-c", "print(1)"], work_path, venv_path)

    # Then: try-flags present; proxies never appear on argv
    assert cmd is not None
    assert "--unshare-user-try" in cmd
    assert "--unshare-user" not in cmd
    assert "--unshare-cgroup-try" in cmd
    assert [f for f in cmd if f.startswith("--unshare-cgroup")] == ["--unshare-cgroup-try"]
    assert "--unshare-net" in cmd
    assert "--chdir" in cmd
    assert cmd[cmd.index("--chdir") + 1] == "/workspace"


def test_resolve_exec_timeout_maps_legacy_timeout_kwarg() -> None:
    kwargs: dict[str, object] = {"timeout": 5, "on_output": None}
    assert resolve_exec_timeout(30, kwargs) == 5
    assert "timeout" not in kwargs
    assert resolve_exec_timeout(12, {"timeout": 5}) == 12
    assert resolve_exec_timeout(30, {}) == 30


def test_sandbox_config_proxy_parsing() -> None:
    data = {
        "http_proxy": "http://10.0.0.1:3128",
        "https_proxy": "http://10.0.0.1:3128",
        "no_proxy": ".local,10.0.0.0/8",
    }
    config = SandboxConfig.from_dict(data)
    assert config.http_proxy == "http://10.0.0.1:3128"
    assert config.https_proxy == "http://10.0.0.1:3128"
    assert config.no_proxy == ".local,10.0.0.0/8"


def test_sandbox_config_proxy_from_dict_fallback_and_empty() -> None:
    fallback = SandboxConfig(
        http_proxy="http://fallback:1",
        https_proxy="http://fallback:2",
        no_proxy="fallback.local",
    )
    # Empty string falls through to fallback
    config = SandboxConfig.from_dict({"http_proxy": ""}, fallback_config=fallback)
    assert config.http_proxy == "http://fallback:1"
    assert config.https_proxy == "http://fallback:2"
    # Partial override
    config2 = SandboxConfig.from_dict({"https_proxy": "http://override:443"}, fallback_config=fallback)
    assert config2.http_proxy == "http://fallback:1"
    assert config2.https_proxy == "http://override:443"
    assert config2.no_proxy == "fallback.local"


def test_sandbox_config_resolve_proxy_env_gates_on_allow_network() -> None:
    with_net = SandboxConfig(allow_network=True, http_proxy="http://p:1")
    without_net = SandboxConfig(allow_network=False, http_proxy="http://p:1")
    assert with_net.resolve_proxy_env() == {"http_proxy": "http://p:1", "HTTP_PROXY": "http://p:1"}
    assert without_net.resolve_proxy_env() == {}


def test_get_sandbox_config_uses_sandbox_proxy_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_sandbox_config, get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("SANDBOX_HTTP_PROXY", "http://sandbox-only:8080")
    monkeypatch.setenv("SANDBOX_HTTPS_PROXY", "http://sandbox-only:8443")
    monkeypatch.setenv("SANDBOX_NO_PROXY", "sandbox.local")
    monkeypatch.setenv("HTTP_PROXY", "http://global-should-not-apply:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://global-should-not-apply:9")
    monkeypatch.setenv("NO_PROXY", "global.local")
    get_settings.cache_clear()

    try:
        config = get_sandbox_config()
        assert config.http_proxy == "http://sandbox-only:8080"
        assert config.https_proxy == "http://sandbox-only:8443"
        assert config.no_proxy == "sandbox.local"
    finally:
        get_settings.cache_clear()


def test_get_sandbox_config_does_not_inherit_global_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_sandbox_config, get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("SANDBOX_HTTP_PROXY", raising=False)
    monkeypatch.delenv("SANDBOX_HTTPS_PROXY", raising=False)
    monkeypatch.delenv("SANDBOX_NO_PROXY", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://global-only:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://global-only:9")
    monkeypatch.setenv("NO_PROXY", "global.local")
    get_settings.cache_clear()

    try:
        config = get_sandbox_config()
        assert config.http_proxy is None
        assert config.https_proxy is None
        assert config.no_proxy is None
    finally:
        get_settings.cache_clear()
