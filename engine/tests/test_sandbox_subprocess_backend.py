import asyncio
import itertools
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.services.sandbox.config import SandboxConfig, SandboxConfigOverrides, SandboxType
from app.services.sandbox.local.subprocess_backend import SubprocessBackend
from app.services.sandbox.registry import get_sandbox_backend


class BlockingStream:
    async def read(self, _: int) -> bytes:
        return b""


class BlockingProcess:
    def __init__(self):
        self.pid: int = 999_999
        self.returncode: int | None = None
        self.started: asyncio.Event = asyncio.Event()
        self.finished: asyncio.Event = asyncio.Event()
        self.stdout: BlockingStream = BlockingStream()
        self.stderr: BlockingStream = BlockingStream()
        self.killed: bool = False
        self.reaped: bool = False

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


@pytest.mark.parametrize("sandbox_type", list(SandboxType))
@pytest.mark.parametrize("exec_timeout", [30, 12])
async def test_execute_rejects_old_timeout_keyword(
    sandbox_type: SandboxType, exec_timeout: int, tmp_path: Path
) -> None:
    # Given: any configured backend, including explicit canonical timeout overrides
    backend = get_sandbox_backend(
        SandboxConfig(type=sandbox_type, api_key="test-key", api_url="http://sandbox.invalid")
    )

    # When / Then: the retired keyword fails before execution or provider access
    with pytest.raises(TypeError, match=r"timeout.*exec_timeout"):
        await backend.execute(
            "print(1)", "python", exec_timeout=exec_timeout, work_dir=str(tmp_path), timeout=5, on_output=None
        )


async def test_execute_enforces_canonical_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a sandbox child that cannot complete within a zero-second execution budget
    backend = SubprocessBackend(SandboxConfig())
    process = BlockingProcess()

    async def ensure_venv(_: Path) -> None:
        return None

    async def launch(*_: object, **__: object) -> BlockingProcess:
        return process

    monkeypatch.setattr(backend, "_ensure_workspace_venv", ensure_venv)
    monkeypatch.setattr(backend, "_build_bwrap_command", lambda *_: ["sandbox"])
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.asyncio.create_subprocess_exec", launch)

    # When: the canonical execution timeout is passed with supported output context
    result = await backend.execute("print(1)", "python", exec_timeout=0, work_dir=str(tmp_path), on_output=None)

    # Then: the timeout is enforced and cleanup preserves the execution contract
    assert not result.success
    assert result.exit_code == 124
    assert process.killed
    assert process.reaped
    assert not (tmp_path / "_exec_tmp.py").exists()


def test_sandbox_config_proxy_parsing() -> None:
    data: SandboxConfigOverrides = {
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


@pytest.mark.parametrize("field", ["max_stdout_bytes", "max_stderr_bytes"])
@pytest.mark.parametrize("value", [-1, True, False, 1.5, "4", None])
def test_output_budget_rejects_invalid_configuration(field: str, value: object) -> None:
    # Given: a boundary value that is not a nonnegative integer.
    # When / Then
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({field: value})


def test_output_budget_defaults_and_field_fallback() -> None:
    # Given: distinct fallback limits.
    fallback = SandboxConfig(max_stdout_bytes=17, max_stderr_bytes=31)
    # When
    defaults = SandboxConfig.from_dict({})
    partial = SandboxConfig.from_dict({"max_stdout_bytes": 0}, fallback)
    # Then: omitted settings use defaults or their own fallback, not the other stream's limit.
    assert (defaults.max_stdout_bytes, defaults.max_stderr_bytes) == (1_000_000, 500_000)
    assert (partial.max_stdout_bytes, partial.max_stderr_bytes) == (0, 31)


class ChunkStream:
    def __init__(self, chunks: Iterator[bytes], finished: asyncio.Event) -> None:
        self.chunks = chunks
        self.finished = finished
        self.exhausted = asyncio.Event()
        self.read_count = 0

    async def read(self, size: int) -> bytes:
        chunk = next(self.chunks, b"")
        self.read_count += 1
        if chunk:
            assert len(chunk) <= size
            return chunk
        self.exhausted.set()
        await self.finished.wait()
        return b""


class OutputProcess:
    def __init__(self, stdout: Iterator[bytes], stderr: Iterator[bytes], *, running: bool = False) -> None:
        self.pid = 999_999
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.killed = False
        self.reaped = False
        self.stdout = ChunkStream(stdout, self.finished)
        self.stderr = ChunkStream(stderr, self.finished)
        self.running = running

    async def wait(self) -> int:
        self.started.set()
        if not self.running:
            await self.stdout.exhausted.wait()
            await self.stderr.exhausted.wait()
            self.returncode = 0
            self.finished.set()
        await self.finished.wait()
        self.reaped = True
        return self.returncode if self.returncode is not None else -9

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.finished.set()


def streaming_backend(
    monkeypatch: pytest.MonkeyPatch, config: SandboxConfig, process: OutputProcess
) -> SubprocessBackend:
    backend = SubprocessBackend(config)

    async def ensure_venv(_: Path) -> None:
        return None

    async def launch(*_: object, **__: object) -> OutputProcess:
        return process

    monkeypatch.setattr(backend, "_ensure_workspace_venv", ensure_venv)
    monkeypatch.setattr(backend, "_build_bwrap_command", lambda *_: ["sandbox"])
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.asyncio.create_subprocess_exec", launch)
    return backend


@pytest.mark.parametrize(
    ("stdout_limit", "stderr_limit", "expected_stdout", "expected_stderr"),
    [(0, 0, "", ""), (3, 1, "A\ufffd", "\ufffd"), (7, 4, "A\u20acBCD", "\u00e9\ufffd!")],
)
async def test_execute_retains_raw_byte_prefixes_and_drains_both_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout_limit: int,
    stderr_limit: int,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    # Given: code points cross read boundaries, with invalid bytes and asymmetric budgets.
    process = OutputProcess(iter((b"A\xe2", b"\x82\xacBCDE")), iter((b"\xc3", b"\xa9\xff!end")))
    backend = streaming_backend(
        monkeypatch, SandboxConfig(max_stdout_bytes=stdout_limit, max_stderr_bytes=stderr_limit), process
    )
    chunks: list[tuple[str, str]] = []
    stderr_seen = asyncio.Event()

    async def on_output(text: str, label: str) -> None:
        chunks.append((text, label))
        if label == "stderr":
            stderr_seen.set()
        else:
            await stderr_seen.wait()

    # When
    result = await backend.execute("pass", "python", work_dir=str(tmp_path), on_output=on_output)
    # Then: clipping cannot fabricate a process error, and callback delivery is independent of capture.
    assert result.success
    assert (result.stdout, result.stderr) == (expected_stdout, expected_stderr)
    assert process.stdout.read_count == process.stderr.read_count == 3
    assert len(chunks) == 4
    assert [label for _, label in chunks] == ["stdout", "stderr", "stderr", "stdout"]
    assert not (tmp_path / "_exec_tmp.py").exists()


async def test_execute_large_output_remains_bounded_and_preserves_display_clipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: each pipe emits eight MiB, far more than either retained budget.
    process = OutputProcess(itertools.repeat(b"s" * 4096, 2048), itertools.repeat(b"e" * 4096, 2048))
    backend = streaming_backend(monkeypatch, SandboxConfig(max_stdout_bytes=12000, max_stderr_bytes=7000), process)
    # When
    tracemalloc.start()
    try:
        result = await backend.execute("pass", "python", work_dir=str(tmp_path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Then: both pipes are exhausted without retaining the full output or changing final display limits.
    assert result.success
    assert result.stdout == "s" * 10000
    assert result.stderr == "e" * 5000
    assert process.stdout.read_count == process.stderr.read_count == 2049
    assert peak < 2_000_000


@pytest.mark.parametrize("cancel", [False, True])
async def test_execute_over_budget_preserves_timeout_and_cancellation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    # Given: a child remains alive after exhausting both retention limits.
    process = OutputProcess(itertools.repeat(b"s" * 4096, 8), itertools.repeat(b"e" * 4096, 8), running=True)
    backend = streaming_backend(monkeypatch, SandboxConfig(max_stdout_bytes=3, max_stderr_bytes=2), process)
    # When: it is either cancelled after draining, or immediately exceeds its timeout.
    if cancel:
        execution = asyncio.create_task(backend.execute("pass", "python", work_dir=str(tmp_path)))
        await process.started.wait()
        await process.stdout.exhausted.wait()
        await process.stderr.exhausted.wait()
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
    else:
        result = await backend.execute("pass", "python", exec_timeout=0, work_dir=str(tmp_path))
        assert result.exit_code == 124
        assert (result.stdout, result.stderr) == ("sss", "ee")
    # Then: the process is reaped and its temporary script is removed even after overflow.
    assert process.killed
    assert process.reaped
    assert not (tmp_path / "_exec_tmp.py").exists()


async def test_execute_cancellation_joins_output_readers_after_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: both readers block in callbacks after exhausting their retention budgets.
    process = OutputProcess(iter((b"stdout overflow",)), iter((b"stderr overflow",)), running=True)
    backend = streaming_backend(monkeypatch, SandboxConfig(max_stdout_bytes=1, max_stderr_bytes=1), process)
    callbacks_started = asyncio.Event()
    release_callbacks = asyncio.Event()
    active_callbacks = 0

    async def on_output(_: str, __: str) -> None:
        nonlocal active_callbacks
        active_callbacks += 1
        if active_callbacks == 2:
            callbacks_started.set()
        try:
            await release_callbacks.wait()
        finally:
            active_callbacks -= 1

    execution = asyncio.create_task(backend.execute("pass", "python", work_dir=str(tmp_path), on_output=on_output))
    try:
        await process.started.wait()
        await callbacks_started.wait()
        # When
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        # Then: no reader survives execution cancellation, even if its callback was suspended.
        assert active_callbacks == 0
        assert process.killed
        assert process.reaped
        assert not (tmp_path / "_exec_tmp.py").exists()
    finally:
        release_callbacks.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
