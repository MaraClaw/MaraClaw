import asyncio
import copy
import itertools
import os
import resource
import signal
import sys
import tracemalloc
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import override
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.services.sandbox.base import ExecutionDeadline, TerminationReason
from app.services.sandbox.config import SandboxConfig, SandboxConfigOverrides, SandboxType
from app.services.sandbox.local import subprocess_backend
from app.services.sandbox.local.subprocess_backend import SubprocessBackend, _terminate_process_group, _TransportProcess
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


def leftover_scripts(work_dir: Path) -> list[Path]:
    return list(work_dir.glob("_exec_tmp*"))


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
    assert leftover_scripts(tmp_path) == []


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

    # Then: setup is part of the deadline, so an already-expired call never launches a child.
    assert not result.success
    assert result.exit_code == 124
    assert not process.started.is_set()
    assert not process.killed
    assert leftover_scripts(tmp_path) == []


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
    assert leftover_scripts(tmp_path) == []


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
        result = await backend.execute(
            "pass", "python", exec_timeout=0, work_dir=str(tmp_path), deadline=ExecutionDeadline(0.1, grace=0.2)
        )
        assert result.exit_code == 124
        assert (result.stdout, result.stderr) == ("sss", "ee")
    # Then: the process is reaped and its temporary script is removed even after overflow.
    assert process.killed
    assert process.reaped
    assert leftover_scripts(tmp_path) == []


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
        assert leftover_scripts(tmp_path) == []
    finally:
        release_callbacks.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)


@pytest.mark.parametrize("fails", [False, True])
async def test_stalled_or_failing_output_delivery_does_not_block_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    process = OutputProcess(iter((b"first", b"second")), iter((b"warning",)))
    backend = streaming_backend(monkeypatch, SandboxConfig(max_stdout_bytes=9, max_stderr_bytes=3), process)
    active: set[asyncio.Task[object]] = set()
    entered = asyncio.Event()

    async def callback(_: str, __: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        active.add(task)
        entered.set()
        try:
            if fails:
                raise RuntimeError("stream disconnected")
            await asyncio.Event().wait()
        finally:
            active.remove(task)

    async with asyncio.timeout(1):
        result = await backend.execute(
            "pass", "python", work_dir=str(tmp_path), on_output=callback, deadline=ExecutionDeadline(0.5, grace=0.2)
        )
    assert entered.is_set()
    assert result.success
    assert (result.stdout, result.stderr) == ("firstseco", "war")
    assert result.output_dropped
    assert active == set()
    assert process.reaped


async def test_terminate_group_kills_survivor_after_leader_exited() -> None:
    leader = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import os; os.read(0, 1)", stdin=asyncio.subprocess.PIPE, process_group=0
    )
    member: asyncio.subprocess.Process | None = None
    try:
        member = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import signal; print('ready', flush=True); signal.pause()",
            stdout=asyncio.subprocess.PIPE,
            process_group=leader.pid,
        )
        assert member.stdout is not None
        async with asyncio.timeout(2):
            assert await member.stdout.readline() == b"ready\n"
            assert leader.stdin is not None
            leader.stdin.close()
            assert await leader.wait() == 0
            await _terminate_process_group(leader, ExecutionDeadline(0, grace=0.2))
            assert await member.wait() == -signal.SIGKILL
    finally:
        for process in (leader, member):
            if process is not None:
                if process.returncode is None:
                    process.kill()
                await process.wait()


async def test_timeout_result_includes_output_drained_after_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = OutputProcess(iter(()), iter(()), running=True)
    backend = streaming_backend(monkeypatch, SandboxConfig(max_stdout_bytes=9), process)

    class AfterKillStream:
        emitted = False

        async def read(self, _: int) -> bytes:
            await process.finished.wait()
            if self.emitted:
                return b""
            self.emitted = True
            return b"late output"

    monkeypatch.setattr(process, "stdout", AfterKillStream())
    async with asyncio.timeout(1):
        result = await backend.execute(
            "pass", "python", work_dir=str(tmp_path), deadline=ExecutionDeadline(0.1, grace=0.2)
        )
    assert result.stdout == "late outp"
    assert result.termination == TerminationReason.SANDBOX_TIMEOUT
    assert process.killed
    assert process.reaped


async def test_execute_closes_pipe_held_by_escaped_setsid_process_after_leader_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    escaped = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import os\ntry:\n while True: os.write(1, b'x' * 4096)\nexcept BrokenPipeError: pass",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert escaped.stdout is not None
    assert os.getsid(escaped.pid) == escaped.pid
    process = OutputProcess(iter(()), iter(()))
    backend = streaming_backend(monkeypatch, SandboxConfig(max_stdout_bytes=17, max_stderr_bytes=3), process)
    monkeypatch.setattr(process, "stdout", escaped.stdout)
    assert isinstance(escaped, _TransportProcess)
    monkeypatch.setattr(process, "_transport", escaped._transport, raising=False)
    process.returncode = 0

    async def exited() -> int:
        return 0

    monkeypatch.setattr(process, "wait", exited)
    before = asyncio.all_tasks()
    try:
        async with asyncio.timeout(2):
            result = await backend.execute(
                "pass", "python", work_dir=str(tmp_path), deadline=ExecutionDeadline(0.1, grace=0.4)
            )
            assert await escaped.wait() == 0
        assert result.termination == TerminationReason.SANDBOX_TIMEOUT
        assert result.stdout == "x" * 17
        assert asyncio.all_tasks() == before
    finally:
        if escaped.returncode is None:
            escaped.kill()
        await escaped.wait()


class LimitRecorder:
    """Records requested rlimits and stands in for the process-changing calls a preexec hook makes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, failing: int | None = None) -> None:
        self.limits: list[tuple[int, tuple[int, int]]] = []
        self.chroots: list[Path] = []
        self.failing = failing
        module = "app.services.sandbox.local.subprocess_backend"
        monkeypatch.setattr(f"{module}.resource.setrlimit", self.setrlimit)
        monkeypatch.setattr(f"{module}.os.chroot", self.chroots.append)
        monkeypatch.setattr(f"{module}.os.geteuid", lambda: 0)
        for name in ("chdir", "umask", "setgid", "setuid"):
            monkeypatch.setattr(f"{module}.os.{name}", lambda *_: 0)

    def setrlimit(self, limit: int, values: tuple[int, int]) -> None:
        self.limits.append((limit, values))
        if limit == self.failing:
            raise ValueError("limit rejected")


def launching_backend(
    monkeypatch: pytest.MonkeyPatch, config: SandboxConfig, bwrap: list[str] | None
) -> tuple[SubprocessBackend, list[tuple[tuple[object, ...], dict[str, object]]]]:
    backend = SubprocessBackend(config)
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def ensure_venv(_: Path) -> None:
        return None

    async def launch(*args: object, **kwargs: object) -> OutputProcess:
        launches.append((args, kwargs))
        return OutputProcess(iter(()), iter(()))

    monkeypatch.setattr(backend, "_ensure_workspace_venv", ensure_venv)
    monkeypatch.setattr(backend, "_build_bwrap_command", lambda command, *_: None if bwrap is None else bwrap + command)
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.asyncio.create_subprocess_exec", launch)
    return backend, launches


def run_preexec(launch: tuple[tuple[object, ...], dict[str, object]]) -> None:
    preexec = launch[1]["preexec_fn"]
    assert callable(preexec)
    preexec()


async def test_bwrap_launch_carries_memory_and_cpu_ceilings_without_nproc_or_chroot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a 1g memory ceiling, a proxy that must stay off argv, and a root effective UID.
    config = SandboxConfig(memory_limit="1g", max_timeout=60, allow_network=True, http_proxy="http://user:pw@proxy:1")
    backend, launches = launching_backend(monkeypatch, config, ["bwrap"])
    recorder = LimitRecorder(monkeypatch)
    # When
    result = await backend.execute("print(1)", "python", exec_timeout=7, work_dir=str(tmp_path))
    run_preexec(launches[0])
    # Then: bwrap (and everything it starts) gets the data ceiling and a CPU ceiling equal to the timeout, nothing else.
    assert result.success
    assert recorder.limits == [
        (resource.RLIMIT_DATA, (1024**3, 1024**3)),
        (resource.RLIMIT_CPU, (7, 7)),
    ]
    assert recorder.chroots == []
    args, kwargs = launches[0]
    assert all("proxy" not in str(arg) for arg in args)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["http_proxy"] == "http://user:pw@proxy:1"


async def test_bwrap_cpu_ceiling_never_exceeds_max_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a requested timeout above the configured maximum.
    backend, launches = launching_backend(monkeypatch, SandboxConfig(max_timeout=5), ["bwrap"])
    recorder = LimitRecorder(monkeypatch)
    # When
    _ = await backend.execute("print(1)", "python", exec_timeout=30, work_dir=str(tmp_path))
    run_preexec(launches[0])
    # Then: the default 256m ceiling and the capped CPU seconds are requested.
    assert recorder.limits == [
        (resource.RLIMIT_DATA, (256 * 1024**2, 256 * 1024**2)),
        (resource.RLIMIT_CPU, (5, 5)),
    ]


def test_bwrap_node_command_runs_without_v8_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: node resolved on the host path.
    monkeypatch.setattr("app.services.sandbox.local.subprocess_backend.shutil.which", lambda _: "/usr/bin/node")
    # When
    command = SubprocessBackend(SandboxConfig())._build_command("node", "/workspace/_exec_tmp.js")
    # Then: the JIT stays on (--jitless removes WebAssembly, which fetch() needs).
    assert command == ["/usr/bin/node", "/workspace/_exec_tmp.js"]


async def test_fallback_applies_every_limit_when_one_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: bubblewrap is missing, unsafe fallback is enabled and the address-space limit is rejected.
    config = SandboxConfig(memory_limit="1g", max_timeout=60, allow_unsafe_fallback_when_bwrap_missing=True)
    backend, launches = launching_backend(monkeypatch, config, None)
    recorder = LimitRecorder(monkeypatch, failing=resource.RLIMIT_AS)
    # When
    result = await backend.execute("print(1)", "python", exec_timeout=7, work_dir=str(tmp_path))
    run_preexec(launches[0])
    # Then: the remaining limits are still requested and root still chroots into the workspace.
    assert result.success
    assert recorder.limits == [
        (resource.RLIMIT_CPU, (7, 7)),
        (resource.RLIMIT_AS, (1024**3, 1024**3)),
        (resource.RLIMIT_FSIZE, (10 * 1024**2, 10 * 1024**2)),
        (resource.RLIMIT_NOFILE, (64, 64)),
        (resource.RLIMIT_NPROC, (32, 32)),
        (resource.RLIMIT_CORE, (0, 0)),
    ]
    assert [str(path) for path in recorder.chroots] == [launches[0][1]["cwd"]]


async def test_missing_bwrap_fails_closed_without_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: bubblewrap is missing and the unsafe fallback is not enabled.
    backend, launches = launching_backend(monkeypatch, SandboxConfig(), None)
    # When
    result = await backend.execute("print(1)", "python", exec_timeout=7, work_dir=str(tmp_path))
    # Then
    assert not result.success
    assert launches == []


MODULE = "app.services.sandbox.local.subprocess_backend"
CEILING = 256 * 1024**2
DEFAULT_VMDATA_KIB = 312_708
JITLESS_VMDATA_KIB = 50_040
RESERVATION = (DEFAULT_VMDATA_KIB - JITLESS_VMDATA_KIB) * 1024


class ProbeProcess(BlockingProcess):
    """A VmData probe that prints `output` and exits with `returncode`, after `release` when one is given."""

    def __init__(self, output: bytes, returncode: int = 0, release: asyncio.Event | None = None) -> None:
        super().__init__()
        self.output = output
        self.exit_code = returncode
        self.release = release

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.release is not None:
            await self.release.wait()
        self.returncode = self.exit_code
        return self.output, b""


def vmdata(kib: int) -> ProbeProcess:
    return ProbeProcess(f"{kib}\n".encode())


class NodeProbes:
    """Serves `node ... -e <script>` probes in order; every other launch reaches the fake installed before it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, probes: list[ProbeProcess]) -> None:
        self.probes = iter(probes)
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.node_lookups = 0
        self.both_looked_up = asyncio.Event()
        self.sandbox_launch: Callable[..., Awaitable[object]] = asyncio.create_subprocess_exec
        monkeypatch.setattr(f"{MODULE}.asyncio.create_subprocess_exec", self.launch)
        monkeypatch.setattr(f"{MODULE}.shutil.which", self.which)
        monkeypatch.setattr(SubprocessBackend, "_node_jit_reservation_bytes", None)
        monkeypatch.setattr(SubprocessBackend, "_node_jit_lock", asyncio.Lock())

    async def launch(self, *args: object, **kwargs: object) -> object:
        if "-e" not in args:
            return await self.sandbox_launch(*args, **kwargs)
        self.calls.append((args, kwargs))
        return next(self.probes)

    def which(self, name: str) -> str:
        if name == "node":
            self.node_lookups += 1
            if self.node_lookups == 2:
                self.both_looked_up.set()
        return f"/usr/bin/{name}"


async def test_bwrap_node_data_ceiling_adds_measured_jit_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a proxy-enabled config and a node whose default boot holds more private memory than --jitless.
    config = SandboxConfig(allow_network=True, http_proxy="http://user:pw@proxy:1")
    backend, launches = launching_backend(monkeypatch, config, ["bwrap"])
    probes = NodeProbes(monkeypatch, [vmdata(DEFAULT_VMDATA_KIB), vmdata(JITLESS_VMDATA_KIB)])
    recorder = LimitRecorder(monkeypatch)
    # When
    result = await backend.execute("console.log(1)", "node", exec_timeout=7, work_dir=str(tmp_path))
    run_preexec(launches[0])
    # Then: the sandboxed node's data ceiling is the configured one plus the measured difference.
    assert result.success
    assert [args[:2] for args, _ in probes.calls] == [("/usr/bin/node", "-e"), ("/usr/bin/node", "--jitless")]
    envs = [kwargs["env"] for _, kwargs in probes.calls]
    assert all(isinstance(env, dict) and "http_proxy" not in env for env in envs)
    assert recorder.limits == [
        (resource.RLIMIT_DATA, (CEILING + RESERVATION, CEILING + RESERVATION)),
        (resource.RLIMIT_CPU, (7, 7)),
    ]


@pytest.mark.parametrize(("language", "code"), [("python", "print(1)"), ("bash", "echo 1")])
async def test_bwrap_python_and_bash_keep_plain_ceiling_without_measurement(
    language: str, code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    backend, launches = launching_backend(monkeypatch, SandboxConfig(), ["bwrap"])
    probes = NodeProbes(monkeypatch, [])
    recorder = LimitRecorder(monkeypatch)
    # When
    result = await backend.execute(code, language, exec_timeout=7, work_dir=str(tmp_path))
    run_preexec(launches[0])
    # Then
    assert result.success
    assert probes.calls == []
    assert recorder.limits[0] == (resource.RLIMIT_DATA, (CEILING, CEILING))


async def test_node_jit_reservation_is_measured_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: two backend instances, as every execute_code call builds its own.
    first, launches = launching_backend(monkeypatch, SandboxConfig(), ["bwrap"])
    probes = NodeProbes(monkeypatch, [vmdata(DEFAULT_VMDATA_KIB), vmdata(JITLESS_VMDATA_KIB)])
    recorder = LimitRecorder(monkeypatch)
    # When
    results = [
        await backend.execute("console.log(1)", "node", exec_timeout=7, work_dir=str(tmp_path))
        for backend in (first, copy.copy(first))
    ]
    for launch in launches:
        run_preexec(launch)
    # Then
    assert [result.success for result in results] == [True, True]
    assert len(probes.calls) == 2
    assert [values for limit, values in recorder.limits if limit == resource.RLIMIT_DATA] == [
        (CEILING + RESERVATION, CEILING + RESERVATION)
    ] * 2


async def test_concurrent_first_node_executions_share_one_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the first probe keeps running until both executions have reached the measurement.
    backend, launches = launching_backend(monkeypatch, SandboxConfig(), ["bwrap"])
    release = asyncio.Event()
    held = ProbeProcess(f"{DEFAULT_VMDATA_KIB}\n".encode(), release=release)
    probes = NodeProbes(monkeypatch, [held, vmdata(JITLESS_VMDATA_KIB)])
    # When
    executions = [
        asyncio.create_task(backend.execute("console.log(1)", "node", exec_timeout=7, work_dir=str(tmp_path / name)))
        for name in ("a", "b")
    ]
    await asyncio.wait_for(probes.both_looked_up.wait(), timeout=5)
    release.set()
    results = await asyncio.gather(*executions)
    # Then
    assert [result.success for result in results] == [True, True]
    assert len(probes.calls) == 2
    assert len(launches) == 2


@pytest.mark.parametrize(
    ("failing_probe", "probe_timeout"),
    [
        pytest.param(lambda: ProbeProcess(b"", returncode=1), 5.0, id="exit"),
        pytest.param(lambda: ProbeProcess(b"VmData: unknown\n"), 5.0, id="garbage"),
        pytest.param(lambda: ProbeProcess(b"", release=asyncio.Event()), 0.0, id="timeout"),
    ],
)
async def test_failed_jit_measurement_launches_nothing_and_is_not_cached(
    failing_probe: Callable[[], ProbeProcess],
    probe_timeout: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the first measurement fails; a later one succeeds.
    backend, launches = launching_backend(monkeypatch, SandboxConfig(), ["bwrap"])
    failing = failing_probe()
    probes = NodeProbes(monkeypatch, [failing, vmdata(DEFAULT_VMDATA_KIB), vmdata(JITLESS_VMDATA_KIB)])
    default_timeout = subprocess_backend._NODE_PROBE_TIMEOUT_SECONDS
    monkeypatch.setattr(f"{MODULE}._NODE_PROBE_TIMEOUT_SECONDS", probe_timeout)
    # When
    failed = await backend.execute("console.log(1)", "node", exec_timeout=7, work_dir=str(tmp_path))
    launched_after_failure = len(launches)
    monkeypatch.setattr(f"{MODULE}._NODE_PROBE_TIMEOUT_SECONDS", default_timeout)
    retried = await backend.execute("console.log(1)", "node", exec_timeout=7, work_dir=str(tmp_path))
    # Then: nothing ran without a measured ceiling, the probe was not left running and the next call measured again.
    assert (failed.success, launched_after_failure) == (False, 0)
    assert failing.returncode is not None
    assert retried.success
    assert (len(probes.calls), len(launches)) == (3, 1)


async def test_jitless_boot_larger_than_default_clamps_reservation_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: --jitless reports more private memory than the default boot.
    backend, launches = launching_backend(monkeypatch, SandboxConfig(), ["bwrap"])
    _ = NodeProbes(monkeypatch, [vmdata(JITLESS_VMDATA_KIB), vmdata(DEFAULT_VMDATA_KIB)])
    recorder = LimitRecorder(monkeypatch)
    # When
    result = await backend.execute("console.log(1)", "node", exec_timeout=7, work_dir=str(tmp_path))
    run_preexec(launches[0])
    # Then
    assert result.success
    assert recorder.limits[0] == (resource.RLIMIT_DATA, (CEILING, CEILING))


class GatedProcess(OutputProcess):
    """A child that stays alive until `gate` opens."""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(iter(()), iter(()))
        self.gate = gate

    @override
    async def wait(self) -> int:
        await self.gate.wait()
        return await super().wait()


@pytest.mark.parametrize("bwrap", [["bwrap"], None], ids=["bwrap", "host-fallback"])
async def test_concurrent_executions_in_one_workspace_run_their_own_script(
    bwrap: list[str] | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: two executions share one work dir and neither child exits before both were launched.
    backend, _ = launching_backend(monkeypatch, SandboxConfig(allow_unsafe_fallback_when_bwrap_missing=True), bwrap)
    gate = asyncio.Event()
    both_launched = asyncio.Event()
    launched: list[tuple[str, str]] = []

    async def launch(*args: object, **_: object) -> GatedProcess:
        script = tmp_path / Path(str(args[-1])).name
        launched.append((script.name, script.read_text(encoding="utf-8")))
        if len(launched) == 2:
            both_launched.set()
        return GatedProcess(gate)

    monkeypatch.setattr(f"{MODULE}.asyncio.create_subprocess_exec", launch)
    # When
    executions = [
        asyncio.create_task(backend.execute(code, "python", work_dir=str(tmp_path)))
        for code in ("print('a')", "print('b')")
    ]
    async with asyncio.timeout(5):
        await both_launched.wait()
    gate.set()
    results = await asyncio.gather(*executions)
    # Then: each child was handed a script holding its own code under its own name, and none is left behind.
    assert [result.success for result in results] == [True, True]
    assert sorted(content for _, content in launched) == ["print('a')", "print('b')"]
    assert len({name for name, _ in launched}) == 2
    assert leftover_scripts(tmp_path) == []


class VenvProcess:
    """A `uv venv` child that exits with `exit_code` once finished and then leaves its interpreter behind."""

    def __init__(self, venv_path: Path, *, interpreter_first: bool) -> None:
        self.pid = 999_999
        self.returncode: int | None = None
        self.exit_code = 0
        self.interpreter = venv_path / "bin" / "python"
        self.finished = asyncio.Event()
        if interpreter_first:
            self.create_interpreter()

    def create_interpreter(self) -> None:
        self.interpreter.parent.mkdir(parents=True, exist_ok=True)
        self.interpreter.touch()

    async def wait(self) -> int:
        await self.finished.wait()
        self.returncode = self.exit_code
        if self.exit_code == 0:
            self.create_interpreter()
        return self.exit_code

    def finish(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.finished.set()


class FakeUv:
    """Replaces `uv venv`: every launch becomes a `VenvProcess` the test finishes itself.

    Thread hops run inline so that a call which does not wait for the creation is already done after one loop turn.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, interpreter_first: bool = False) -> None:
        self.interpreter_first = interpreter_first
        self.processes: list[VenvProcess] = []
        self.launches: asyncio.Queue[VenvProcess] = asyncio.Queue()
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda _: "/usr/bin/uv")
        monkeypatch.setattr(f"{MODULE}.asyncio.create_subprocess_exec", self.launch)
        monkeypatch.setattr(f"{MODULE}._terminate_process_group", self.terminate)
        monkeypatch.setattr(f"{MODULE}.asyncio.to_thread", self.inline)

    async def launch(self, *args: object, **_: object) -> VenvProcess:
        process = VenvProcess(Path(str(args[-1])), interpreter_first=self.interpreter_first)
        self.processes.append(process)
        self.launches.put_nowait(process)
        return process

    async def terminate(self, child: VenvProcess, deadline: ExecutionDeadline | None = None) -> None:
        del child, deadline

    async def inline(self, func: Callable[..., object], *args: object, **kwargs: object) -> object:
        return func(*args, **kwargs)

    async def next_launch(self) -> VenvProcess:
        async with asyncio.timeout(5):
            return await self.launches.get()


@pytest.mark.parametrize("interpreter_first", [False, True], ids=["interpreter-last", "interpreter-first"])
async def test_concurrent_venv_calls_share_one_uv_and_wait_for_it(
    interpreter_first: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: uv takes a while to create the venv; real uv lays the interpreter down before seeding, so it may exist early.
    uv = FakeUv(monkeypatch, interpreter_first=interpreter_first)
    backend = SubprocessBackend(SandboxConfig())
    venv = tmp_path / ".venv"
    # When: three executions need the same venv at once.
    calls = [asyncio.create_task(backend._ensure_workspace_venv(venv)) for _ in range(3)]
    creating = await uv.next_launch()
    await asyncio.sleep(0)
    # Then: one uv runs, and nobody takes the half-made venv for ready before it exits.
    assert len(uv.processes) == 1
    assert not any(call.done() for call in calls)
    creating.finish()
    async with asyncio.timeout(5):
        _ = await asyncio.gather(*calls)
    assert len(uv.processes) == 1
    assert SubprocessBackend._venv_creations == {}


@pytest.mark.parametrize("fails", [True, False], ids=["fails", "cancelled"])
async def test_waiting_venv_call_creates_the_venv_itself_when_the_creating_call_ends_without_one(
    fails: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one call is creating the venv and another waits for it.
    uv = FakeUv(monkeypatch)
    backend = SubprocessBackend(SandboxConfig())
    venv = tmp_path / ".venv"
    creator = asyncio.create_task(backend._ensure_workspace_venv(venv))
    failing = await uv.next_launch()
    waiter = asyncio.create_task(backend._ensure_workspace_venv(venv))
    await asyncio.sleep(0)
    assert len(uv.processes) == 1
    # When: the creating call fails or is cancelled.
    if fails:
        failing.finish(exit_code=1)
        with pytest.raises(RuntimeError, match="uv failed"):
            await creator
    else:
        _ = creator.cancel()
        with pytest.raises(asyncio.CancelledError):
            await creator
    # Then: the waiting call neither hangs nor trusts the missing venv: it checks again and creates the venv itself.
    retry = await uv.next_launch()
    retry.finish()
    async with asyncio.timeout(5):
        await waiter
    assert len(uv.processes) == 2
    assert SubprocessBackend._venv_creations == {}


async def test_venv_creation_for_different_paths_does_not_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the venv of one agent is still being created.
    uv = FakeUv(monkeypatch)
    backend = SubprocessBackend(SandboxConfig())
    slow = asyncio.create_task(backend._ensure_workspace_venv(tmp_path / "a" / ".venv"))
    slow_uv = await uv.next_launch()
    # When: another agent needs its own venv.
    other = asyncio.create_task(backend._ensure_workspace_venv(tmp_path / "b" / ".venv"))
    other_uv = await uv.next_launch()
    other_uv.finish()
    # Then: it is ready while the first one is still being created.
    async with asyncio.timeout(5):
        await other
    assert not slow.done()
    slow_uv.finish()
    async with asyncio.timeout(5):
        await slow
