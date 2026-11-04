"""Local subprocess-based sandbox backend."""

import asyncio
import contextlib
import inspect
import os
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import ClassVar, NotRequired, Protocol, TypedDict, override, runtime_checkable

from app.core.logging import logger
from app.services.sandbox.base import (
    BaseSandboxBackend,
    ExecutionDeadline,
    ExecutionResult,
    SandboxCapabilities,
    TerminationReason,
    active_execution_deadline,
)
from app.services.sandbox.config import SandboxConfig
from app.services.workspace_paths import WorkspacePathError, resolve_path_within_root


class SubprocessExecKwargs(TypedDict):
    stdout: int
    stderr: int
    env: dict[str, str]
    start_new_session: bool
    preexec_fn: NotRequired[Callable[[], None]]


@runtime_checkable
class _TransportProcess(Protocol):
    _transport: asyncio.SubprocessTransport


def _close_pipes(process: asyncio.subprocess.Process) -> None:
    # Process.wait() can wait for pipe EOF even after the child was reaped. A setsid descendant may hold those FDs;
    # bubblewrap's PID namespace owns its lifetime, while these local transports must still be closed explicitly.
    if isinstance(process, _TransportProcess):
        for fd in (1, 2):
            pipe = process._transport.get_pipe_transport(fd)
            if pipe is not None:
                pipe.close()


async def _terminate_process_group(
    process: asyncio.subprocess.Process, deadline: ExecutionDeadline | None = None
) -> None:
    """Kill the original session group even when its leader was already reaped; never wait for EOF indefinitely."""
    budget = deadline.step_budget(0.25) if deadline is not None else 0.25
    # start_new_session=True makes PID the original PGID. getpgid(pid) fails after leader exit, losing descendants.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    waiter = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait((waiter,), timeout=budget)
        if not done:
            _close_pipes(process)
    finally:
        _ = waiter.cancel()
        _ = await asyncio.gather(waiter, return_exceptions=True)


# Security patterns - reused from agent_tools.py
_DANGEROUS_BASH_ALWAYS = [
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){ :",
    "chmod 777 /",
    "chown ",
    "shutdown",
    "reboot",
]

_DANGEROUS_BASH_NETWORK = [
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
]

_DANGEROUS_PYTHON_IMPORTS_ALWAYS = [
    "shutil.rmtree",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
]

_DANGEROUS_PYTHON_IMPORTS_NETWORK = [
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
]

_DANGEROUS_NODE_ALWAYS = [
    "fs.rmSync",
    "fs.rmdirSync",
    "process.exit",
]

_DANGEROUS_NODE_NETWORK = ["require('http')", "require('https')", "require('net')"]


def _execute_kwarg(kwargs: Mapping[str, object], name: str) -> object:
    return kwargs.get(name)


async def _emit_output(on_output: object, text: str, label: str) -> None:
    if not callable(on_output):
        return
    maybe = (
        on_output(text, label)
        if inspect.iscoroutinefunction(on_output)
        else await asyncio.to_thread(on_output, text, label)
    )
    if isinstance(maybe, Awaitable):
        await maybe


def _observe_delivery(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        _ = task.exception()


def _check_code_safety(language: str, code: str, allow_network: bool = False) -> str | None:
    """Check code for dangerous patterns. Returns error message if unsafe, None if ok."""
    code_lower = code.lower()

    if language == "bash":
        # Always check dangerous patterns
        for pattern in _DANGEROUS_BASH_ALWAYS:
            if pattern.lower() in code_lower:
                logger.warning(f"Blocked: dangerous command detected ({pattern.strip()})")
                return f"Blocked: dangerous command detected ({pattern.strip()})"
        # Network commands only when network is not allowed
        if not allow_network:
            for pattern in _DANGEROUS_BASH_NETWORK:
                if pattern.lower() in code_lower:
                    logger.warning(f"Blocked: network command not allowed ({pattern.strip()})")
                    return f"Blocked: network command not allowed ({pattern.strip()})"
        if "../../" in code:
            return "Blocked: directory traversal not allowed"

    elif language == "python":
        # Always check dangerous patterns
        for pattern in _DANGEROUS_PYTHON_IMPORTS_ALWAYS:
            if pattern.lower() in code_lower:
                logger.warning(f"Blocked: unsafe operation detected ({pattern.strip()})")
                return f"Blocked: unsafe operation detected ({pattern.strip()})"
        # Network imports only when network is not allowed
        if not allow_network:
            for pattern in _DANGEROUS_PYTHON_IMPORTS_NETWORK:
                if pattern.lower() in code_lower:
                    logger.warning(f"Blocked: network operation not allowed ({pattern.strip()})")
                    return f"Blocked: network operation not allowed ({pattern.strip()})"

    elif language == "node":
        # Always check dangerous patterns
        for pattern in _DANGEROUS_NODE_ALWAYS:
            if pattern.lower() in code_lower:
                return f"Blocked: unsafe operation detected ({pattern})"
        # Network requires only when network is not allowed
        if not allow_network:
            for pattern in _DANGEROUS_NODE_NETWORK:
                if pattern.lower() in code_lower:
                    logger.warning(f"Blocked: network operation not allowed ({pattern.strip()})")
                    return f"Blocked: network operation not allowed ({pattern.strip()})"

    return None


class SubprocessBackend(BaseSandboxBackend):
    """Local subprocess-based sandbox backend.

    This backend executes code in a subprocess within the agent's workspace.
    It requires bubblewrap-based filesystem isolation for execute_code.
    When bubblewrap is unavailable, code execution fails closed.
    """

    _bwrap_missing_warned: ClassVar[bool] = False

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config

    @property
    @override
    def name(self) -> str:
        return "subprocess"

    def _venv_python(self, venv_path: Path) -> str:
        del venv_path
        return "/workspace/.venv/bin/python"

    def _host_venv_python(self, work_path: Path) -> str:
        return str(work_path / ".venv" / "bin" / "python")

    def _build_command(self, language: str, script_path: str) -> list[str]:
        if language == "python":
            return ["/workspace/.venv/bin/python", "-I", "-B", str(script_path)]
        executable = shutil.which("bash") if language == "bash" else shutil.which("node")
        if executable in (None, ""):
            raise RuntimeError(f"{language} executable is unavailable")
        return (
            [executable, "--noprofile", "--norc", str(script_path)]
            if language == "bash"
            else [executable, str(script_path)]
        )

    def _build_host_command(self, language: str, script_path: Path, work_path: Path) -> list[str]:
        if language == "python":
            return [self._host_venv_python(work_path), "-I", "-B", str(script_path)]
        executable = shutil.which("bash") if language == "bash" else shutil.which("node")
        if executable in (None, ""):
            raise RuntimeError(f"{language} executable is unavailable")
        return (
            [executable, "--noprofile", "--norc", str(script_path)]
            if language == "bash"
            else [executable, str(script_path)]
        )

    def _build_safe_env(self, work_path: Path) -> dict[str, str]:
        venv_bin = work_path / ".venv" / "bin"
        workspace_tmp = work_path / ".tmp"
        env = {
            "HOME": str(work_path),
            "PATH": f"{venv_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(workspace_tmp),
            "NODE_PATH": "",
            "BASH_ENV": "",
            "ENV": "",
            "VIRTUAL_ENV": str(work_path / ".venv"),
            "PIP_CACHE_DIR": str(workspace_tmp / "pip-cache"),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        # Proxy via process env only (not bwrap --setenv argv) so URLs with
        # userinfo are not exposed on /proc/<pid>/cmdline.
        env.update(self.config.resolve_proxy_env())
        return env

    def _bind_if_exists(self, host_path: str, guest_path: str | None = None, *, read_only: bool = True) -> list[str]:
        host = Path(host_path)
        if not host.exists():
            return []
        target = guest_path or host_path
        bind_flag = "--ro-bind" if read_only else "--bind"
        return [bind_flag, str(host), target]

    async def _ensure_workspace_venv(self, venv_path: Path) -> None:
        venv_python = venv_path / "bin" / "python"
        if not venv_python.exists():
            uv_executable = shutil.which("uv")
            if uv_executable in (None, ""):
                raise RuntimeError("uv executable is required to create the sandbox virtual environment")
            process = await asyncio.create_subprocess_exec(
                uv_executable,
                "venv",
                "--seed",
                str(venv_path),
                cwd=str(venv_path.parent),
                start_new_session=True,
            )
            try:
                returncode = await process.wait()
            except asyncio.CancelledError:
                await _terminate_process_group(process)
                raise
            if returncode != 0:
                raise RuntimeError("uv failed to create the sandbox virtual environment")

        # Fix shebang lines in pip scripts to use bwrap-visible path
        # venv creates scripts with absolute paths to the host Python,
        # but bwrap only mounts /workspace, so those paths don't exist inside the sandbox
        await asyncio.to_thread(self._fix_pip_shebangs, venv_path)

    def _fix_pip_shebangs(self, venv_path: Path) -> None:
        """Replace pip with a bash wrapper that delegates to uv pip for extreme performance."""
        venv_bin = venv_path / "bin"
        wrapper_script = '#!/bin/bash\nexec uv pip "$@"\n'

        for pip_cmd in ["pip", "pip3", "pip3.12"]:
            pip_path = venv_bin / pip_cmd
            if pip_path.parent.exists():
                _ = pip_path.write_text(wrapper_script, encoding="utf-8")
                pip_path.chmod(0o755)

    def _build_exec_kwargs(self, work_path: Path, timeout: int, use_preexec: bool = False) -> SubprocessExecKwargs:
        kwargs: SubprocessExecKwargs = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": self._build_safe_env(work_path),
            "start_new_session": True,
        }
        if use_preexec:
            kwargs["preexec_fn"] = self._build_preexec_fn(work_path, timeout)
        return kwargs

    def _build_preexec_fn(self, work_path: Path, timeout: int) -> Callable[[], None]:
        def _preexec() -> None:
            os.chdir(work_path)
            _ = os.umask(0o077)

            try:
                import resource

                memory_bytes = int(self.config.memory_limit.rstrip("mM")) * 1024 * 1024
                cpu_limit = max(1, min(timeout, self.config.max_timeout))
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
                resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
                resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
                if hasattr(resource, "RLIMIT_CORE"):
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except Exception as exc:
                logger.warning(f"[Subprocess] Failed to apply resource limits: {exc}")

            if hasattr(os, "setgid"):
                with contextlib.suppress(OSError):
                    os.setgid(os.getgid())
            if hasattr(os, "setuid"):
                with contextlib.suppress(OSError):
                    os.setuid(os.getuid())

            if hasattr(os, "chroot") and os.geteuid() == 0:
                try:
                    os.chroot(work_path)
                    os.chdir("/")
                except Exception as exc:
                    logger.warning(f"[Subprocess] Failed to chroot into workspace: {exc}")

        return _preexec

    def _build_bwrap_command(self, command: list[str], work_path: Path, venv_path: Path) -> list[str] | None:
        bwrap = shutil.which("bwrap")
        if bwrap in (None, ""):
            if not SubprocessBackend._bwrap_missing_warned:
                logger.warning(
                    "[Subprocess] bubblewrap (bwrap) is not available. "
                    "execute_code will be rejected until bubblewrap is installed."
                )
                SubprocessBackend._bwrap_missing_warned = True
            return None

        base_binds = (
            self._bind_if_exists("/usr")
            + self._bind_if_exists("/usr/local")
            + self._bind_if_exists("/bin")
            + self._bind_if_exists("/lib")
            + self._bind_if_exists("/lib64")
            + self._bind_if_exists("/etc")
        )

        cmd = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            # Create a user namespace when the kernel allows it; skip on hosts with
            # user.max_user_namespaces=0 / older kernels (pair with setuid bwrap).
            "--unshare-user-try",
            "--unshare-ipc",
            "--unshare-pid",
            "--unshare-uts",
            # --unshare-cgroup-try skips gracefully on kernels < 4.6
            "--unshare-cgroup-try",
            *base_binds,
            "--bind",
            "/data/agents/.uv-cache",
            "/uv-cache",
            "--bind",
            str(work_path),
            "/workspace",
            "--bind",
            str(venv_path),
            "/workspace/.venv",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(work_path / ".tmp"),
            str(Path("/") / "tmp"),
            "--setenv",
            "HOME",
            "/workspace",
            "--setenv",
            "PATH",
            f"/workspace/.venv/bin:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "--setenv",
            "TMPDIR",
            "/workspace/.tmp",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
            "--setenv",
            "NODE_PATH",
            "",
            "--setenv",
            "BASH_ENV",
            "",
            "--setenv",
            "ENV",
            "",
            "--setenv",
            "VIRTUAL_ENV",
            "/workspace/.venv",
            "--setenv",
            "PIP_CACHE_DIR",
            "/workspace/.tmp/pip-cache",
            "--setenv",
            "PIP_DISABLE_PIP_VERSION_CHECK",
            "1",
            "--setenv",
            "UV_CACHE_DIR",
            "/uv-cache",
            "--chdir",
            "/workspace",
        ]
        # Proxy vars come from the process env built by _build_safe_env (not
        # --setenv) so authenticated proxy URLs stay off the process cmdline.
        if not self.config.allow_network:
            cmd.append("--unshare-net")
        cmd.extend(command)
        return cmd

    @override
    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=256,
            network_available=self.config.allow_network,
            filesystem_available=True,
        )

    @override
    async def health_check(self) -> bool:
        """Check if basic system commands are available."""
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _ = await proc.communicate()
            return proc.returncode == 0
        except Exception:
            return False

    @override
    async def execute(
        self,
        code: str,
        language: str,
        exec_timeout: int = 30,
        work_dir: str | None = None,
        **kwargs: object,
    ) -> ExecutionResult:
        """Bound setup, execution and pipe draining with the same monotonic deadline used by the tool guard."""
        supplied = kwargs.get("deadline")
        deadline = (
            supplied
            if isinstance(supplied, ExecutionDeadline)
            else active_execution_deadline.get() or ExecutionDeadline(exec_timeout)
        )
        try:
            async with asyncio.timeout(deadline.remaining()):
                return await self._execute(
                    code,
                    language,
                    exec_timeout,
                    work_dir,
                    deadline=deadline,
                    **{key: value for key, value in kwargs.items() if key != "deadline"},
                )
        except TimeoutError:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=124,
                duration_ms=deadline.elapsed_ms(),
                error=f"Code execution timed out after {exec_timeout}s",
                termination=TerminationReason.SANDBOX_TIMEOUT,
            )

    async def _execute(
        self,
        code: str,
        language: str,
        exec_timeout: int = 30,
        work_dir: str | None = None,
        *,
        deadline: ExecutionDeadline,
        **kwargs: object,
    ) -> ExecutionResult:
        """Execute code in a subprocess."""
        if "timeout" in kwargs:
            raise TypeError("execute() no longer accepts 'timeout'; use 'exec_timeout'")
        timeout = exec_timeout
        on_output = _execute_kwarg(kwargs, "on_output")
        agent_id = _execute_kwarg(kwargs, "agent_id")

        # Validate language
        if language not in ("python", "bash", "node"):
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error=f"Unsupported language: {language}. Use: python, bash, or node",
            )

        # Security check - pass allow_network config
        safety_error = _check_code_safety(language, code, self.config.allow_network)
        if safety_error not in (None, ""):
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error=f"❌ {safety_error}",
            )

        # Determine work directory and ensure it cannot escape its own root.
        work_path = await asyncio.to_thread(
            Path(work_dir).resolve if work_dir not in (None, "") else (Path.cwd() / "workspace").resolve
        )
        try:
            work_path = resolve_path_within_root(work_path, "", label="work_dir")
        except WorkspacePathError as exc:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error=str(exc),
            )
        await asyncio.to_thread(work_path.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((work_path / ".tmp").mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread((work_path / ".tmp" / "pip-cache").mkdir, parents=True, exist_ok=True)

        # Determine persistent venv path if possible
        if agent_id:
            # We place the virtual environment in a persistent location
            venv_path = await asyncio.to_thread(Path("/data/agents").resolve)
            venv_path = venv_path / str(agent_id) / ".venv"
            await asyncio.to_thread(venv_path.parent.mkdir, parents=True, exist_ok=True)

            # Ensure global uv cache exists
            uv_cache = Path("/data/agents/.uv-cache")
            await asyncio.to_thread(uv_cache.mkdir, parents=True, exist_ok=True)
        else:
            venv_path = work_path / ".venv"

        # Determine command and file extension
        if language == "python":
            ext = ".py"
        elif language == "bash":
            ext = ".sh"
        else:
            ext = ".js"

        # Write code to temp file
        script_path = work_path / f"_exec_tmp{ext}"

        proc: asyncio.subprocess.Process | None = None
        readers: list[asyncio.Task[None]] = []
        deliveries: list[asyncio.Task[None]] = []
        stdout_data = bytearray()
        stderr_data = bytearray()
        output_dropped = False
        result: ExecutionResult | None = None
        try:
            await self._ensure_workspace_venv(venv_path)
            _ = await asyncio.to_thread(script_path.write_text, code, encoding="utf-8")

            sandbox_command = self._build_command(language, f"/workspace/{script_path.name}")
            bwrap_command = self._build_bwrap_command(sandbox_command, work_path, venv_path)
            if bwrap_command is None or not bwrap_command:
                if not self.config.allow_unsafe_fallback_when_bwrap_missing:
                    duration_ms = deadline.elapsed_ms()
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=duration_ms,
                        error=(
                            "bubblewrap (bwrap) is required for execute_code but is not available. "
                            "Install bwrap in the runtime environment or enable "
                            "allow_unsafe_fallback_when_bwrap_missing for local development."
                        ),
                    )

                host_command = self._build_host_command(language, script_path, work_path)
                logger.warning("[Subprocess] bubblewrap missing; using local fallback without filesystem isolation")
                proc = await asyncio.create_subprocess_exec(
                    *host_command,
                    cwd=str(work_path),
                    **self._build_exec_kwargs(work_path, timeout, use_preexec=True),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *bwrap_command,
                    cwd=str(work_path),
                    **self._build_exec_kwargs(work_path, timeout),
                )

            async def deliver(queue: asyncio.Queue[bytes | None], label: str) -> None:
                nonlocal output_dropped
                while (chunk := await queue.get()) is not None:
                    delivery = asyncio.create_task(
                        _emit_output(on_output, chunk.decode("utf-8", errors="replace"), label)
                    )
                    try:
                        done, _ = await asyncio.wait((delivery,), timeout=min(0.1, deadline.remaining()))
                        if not done:
                            output_dropped = True
                            return
                        delivery.result()
                    except Exception:
                        output_dropped = True
                        return
                    finally:
                        if not delivery.done():
                            output_dropped = True
                            _ = delivery.cancel()
                            done, _ = await asyncio.wait((delivery,), timeout=deadline.step_budget(0.05))
                            if not done:
                                delivery.add_done_callback(_observe_delivery)
                        if delivery.done():
                            _observe_delivery(delivery)

            async def read_stream(stream: asyncio.StreamReader | None, out: bytearray, label: str = "stdout") -> None:
                nonlocal output_dropped
                if stream is None:
                    return
                capture_limit = self.config.max_stderr_bytes if label == "stderr" else self.config.max_stdout_bytes
                queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
                delivery = asyncio.create_task(deliver(queue, label)) if on_output else None
                if delivery is not None:
                    deliveries.append(delivery)
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    remaining = capture_limit - len(out)
                    if remaining > 0:
                        out.extend(chunk[:remaining])
                    if delivery is not None:
                        try:
                            if delivery.done():
                                output_dropped = True
                            else:
                                queue.put_nowait(chunk)
                        except asyncio.QueueFull:
                            output_dropped = True
                if delivery is not None and not delivery.done():
                    if queue.full():
                        _ = queue.get_nowait()
                        output_dropped = True
                    queue.put_nowait(None)

            readers = [
                asyncio.create_task(read_stream(proc.stdout, stdout_data, "stdout")),
                asyncio.create_task(read_stream(proc.stderr, stderr_data, "stderr")),
            ]

            _ = await proc.wait()
            _ = await asyncio.wait(readers)
            for reader in readers:
                reader.result()
            if deliveries:
                _ = await asyncio.wait(deliveries)
            stdout = bytes(stdout_data)
            stderr = bytes(stderr_data)

            stdout_str = stdout.decode("utf-8", errors="replace")[:10000] if stdout else ""
            stderr_str = stderr.decode("utf-8", errors="replace")[:5000] if stderr else ""

            duration_ms = deadline.elapsed_ms()

            exit_code = proc.returncode if proc.returncode is not None else 1
            result = ExecutionResult(
                success=proc.returncode == 0,
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
                duration_ms=duration_ms,
                error=None if proc.returncode == 0 else f"Exit code: {proc.returncode}",
                output_dropped=output_dropped,
            )
            return result

        except asyncio.CancelledError:
            if deadline.expired:
                result = ExecutionResult(
                    success=False,
                    stdout=stdout_data.decode("utf-8", errors="replace")[:10000],
                    stderr=stderr_data.decode("utf-8", errors="replace")[:5000],
                    exit_code=124,
                    duration_ms=deadline.elapsed_ms(),
                    error=f"Code execution timed out after {timeout}s",
                    termination=TerminationReason.SANDBOX_TIMEOUT,
                    output_dropped=output_dropped or any(not task.done() for task in deliveries),
                )
                return result
            raise
        except Exception as e:
            duration_ms = deadline.elapsed_ms()
            logger.exception("[Subprocess] Execution error")
            result = ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"Execution error: {str(e)[:200]}",
                termination=TerminationReason.BACKEND_ABORT,
            )
            return result

        finally:
            if proc is not None:
                await _terminate_process_group(proc, deadline)
                if readers:
                    _ = await asyncio.wait(readers, timeout=deadline.step_budget(0.1))
                _close_pipes(proc)
            for task in readers:
                _ = task.cancel()
            _ = await asyncio.gather(*readers, return_exceptions=True)
            for task in deliveries:
                _ = task.cancel()
            if deliveries:
                _, pending = await asyncio.wait(deliveries, timeout=deadline.step_budget(0.1))
                for task in pending:
                    output_dropped = True
                    task.add_done_callback(_observe_delivery)
            if result is not None:
                result.stdout = stdout_data.decode("utf-8", errors="replace")[:10000]
                result.stderr = stderr_data.decode("utf-8", errors="replace")[:5000]
                result.output_dropped |= output_dropped
                result.duration_ms = deadline.elapsed_ms()
            # Clean up temp script
            await asyncio.to_thread(script_path.unlink, missing_ok=True)
