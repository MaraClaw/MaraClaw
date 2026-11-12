"""Code execution through the configured sandbox backend."""

import asyncio
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Final

from app.core.json_types import JsonObject, json_as_str
from app.core.logging import logger
from app.services import agent_tools
from app.services.sandbox.base import ExecutionDeadline, ExecutionResult, TerminationReason, active_execution_deadline
from app.services.sandbox.config import SandboxConfigOverrides
from app.services.tool_runtime.provenance import config_provenance

from .registry import ToolArguments, ToolOutputCallback

SANDBOX_BUSY_MESSAGE: Final = (
    "❌ Sandbox busy: every code execution slot is in use and none freed up in time. Please retry later."
)


class _ExecutionSlots:
    """Process-wide execution slots, shared by every event loop and thread.

    A freed slot passes straight to the oldest waiter, so queued callers keep FIFO order and new arrivals never
    overtake them. Each waiter is a future on its own loop, so no asyncio primitive is tied to one loop.
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._in_use: int = 0
        self._waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self, limit: int, wait_seconds: float) -> bool:
        """Take a slot, waiting at most ``wait_seconds`` for one. A cancelled or expired wait holds nothing."""
        with self._lock:
            if self._in_use < limit and not self._waiters:
                self._in_use += 1
                return True
            if wait_seconds <= 0:
                return False
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
        try:
            async with asyncio.timeout(wait_seconds):
                await waiter
        except TimeoutError:
            self._abandon(waiter)
            return False
        except asyncio.CancelledError:
            self._abandon(waiter)
            raise
        return True

    def release(self) -> None:
        with self._lock:
            if not self._waiters:
                self._in_use -= 1
                return
            waiter = self._waiters.popleft()
        waiter.get_loop().call_soon_threadsafe(self._hand_over, waiter)

    def _hand_over(self, waiter: asyncio.Future[None]) -> None:
        # The waiter gave up after it left the queue: the slot moves on instead of being dropped.
        if waiter.cancelled():
            self.release()
        else:
            waiter.set_result(None)

    def _abandon(self, waiter: asyncio.Future[None]) -> None:
        with self._lock:
            queued = waiter in self._waiters
            if queued:
                self._waiters.remove(waiter)
        # A slot handed over just before the wait ended belongs to this caller, which no longer runs: pass it on.
        # A hand-over still in flight finds the waiter cancelled and passes the slot on itself.
        if not queued and not waiter.cancelled():
            self.release()


_EXECUTION_SLOTS = _ExecutionSlots()


def _string_argument(arguments: ToolArguments, name: str, default: str) -> str:
    value = arguments.get(name, default)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _prepare_work_dir(ws: Path) -> Path:
    work_dir = ws.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _sandbox_overrides(config: JsonObject) -> SandboxConfigOverrides:
    overrides: SandboxConfigOverrides = {}
    sandbox_type = json_as_str(config.get("sandbox_type"))
    if sandbox_type is not None:
        overrides["sandbox_type"] = sandbox_type
    api_key = json_as_str(config.get("api_key"))
    if api_key is not None:
        overrides["api_key"] = api_key
    api_url = json_as_str(config.get("api_url"))
    if api_url is not None:
        overrides["api_url"] = api_url
    cpu_limit = json_as_str(config.get("cpu_limit"))
    if cpu_limit is not None:
        overrides["cpu_limit"] = cpu_limit
    memory_limit = json_as_str(config.get("memory_limit"))
    if memory_limit is not None:
        overrides["memory_limit"] = memory_limit
    allow_network = config.get("allow_network")
    if isinstance(allow_network, bool):
        overrides["allow_network"] = allow_network
    allow_unsafe = config.get("allow_unsafe_fallback_when_bwrap_missing")
    if isinstance(allow_unsafe, bool):
        overrides["allow_unsafe_fallback_when_bwrap_missing"] = allow_unsafe
    default_timeout = config.get("default_timeout")
    if isinstance(default_timeout, int) and not isinstance(default_timeout, bool):
        overrides["default_timeout"] = default_timeout
    max_timeout = config.get("max_timeout")
    if isinstance(max_timeout, int) and not isinstance(max_timeout, bool):
        overrides["max_timeout"] = max_timeout
    max_stdout_bytes = config.get("max_stdout_bytes")
    if isinstance(max_stdout_bytes, int) and not isinstance(max_stdout_bytes, bool):
        overrides["max_stdout_bytes"] = max_stdout_bytes
    max_stderr_bytes = config.get("max_stderr_bytes")
    if isinstance(max_stderr_bytes, int) and not isinstance(max_stderr_bytes, bool):
        overrides["max_stderr_bytes"] = max_stderr_bytes
    http_proxy = json_as_str(config.get("http_proxy"))
    if http_proxy is not None:
        overrides["http_proxy"] = http_proxy
    https_proxy = json_as_str(config.get("https_proxy"))
    if https_proxy is not None:
        overrides["https_proxy"] = https_proxy
    no_proxy = json_as_str(config.get("no_proxy"))
    if no_proxy is not None:
        overrides["no_proxy"] = no_proxy
    return overrides


async def _execute_code(
    agent_id: uuid.UUID | None,
    ws: Path,
    arguments: ToolArguments,
    *,
    tool_name: str = "execute_code",
    on_output: ToolOutputCallback | None = None,
) -> str:
    """Execute code using platform sandbox settings with per-agent overrides."""
    language = _string_argument(arguments, "language", "python")
    code = _string_argument(arguments, "code", "")
    requested_timeout = _integer_argument(arguments, "timeout", 30)

    if not code.strip():
        return "❌ No code provided"

    if language not in ("python", "bash", "node"):
        return f"❌ Unsupported language: {language}. Use: python, bash, or node"

    is_e2b_tool = tool_name == "execute_code_e2b"

    # Import here to avoid circular imports.
    from app.config import get_sandbox_config, get_settings
    from app.services.sandbox.config import SandboxConfig
    from app.services.sandbox.registry import get_sandbox_backend

    try:
        # Platform defaults are configuration inheritance, not an execution retry.
        fallback_config = get_sandbox_config()
        tool_config = await agent_tools._get_tool_config(agent_id, tool_name)

        if tool_config is not None and tool_config:
            # Loader provenance travels beside the JSON overrides, never inside them.
            sandbox_config = SandboxConfig.from_dict(
                _sandbox_overrides(tool_config), fallback_config, provenance=config_provenance(tool_config)
            )
        else:
            sandbox_config = fallback_config
            logger.info(f"[Sandbox] No per-agent config found for '{tool_name}', using fallback")

        # Working directory is the agent's absolute root directory.
        work_dir = _prepare_work_dir(ws)
        timeout = min(requested_timeout, sandbox_config.max_timeout)
        backend = get_sandbox_backend(sandbox_config)
        # Admission follows configuration, so a configuration error never waits for or holds a slot, and precedes the
        # deadline, so time spent queued is not taken from the code's own timeout.
        settings = get_settings()
        slots = _EXECUTION_SLOTS
        if not await slots.acquire(
            settings.SANDBOX_MAX_CONCURRENT_EXECUTIONS, settings.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS
        ):
            logger.warning(
                f"[Sandbox] Execution refused: all {settings.SANDBOX_MAX_CONCURRENT_EXECUTIONS} slots stayed busy for "
                f"{settings.SANDBOX_EXECUTION_QUEUE_WAIT_SECONDS}s (agent={agent_id}, tool={tool_name})"
            )
            return SANDBOX_BUSY_MESSAGE
        logger.info(
            f"[Sandbox] Executing code with backend: {backend.__class__.__name__} (tool={tool_name}, timeout={timeout}s)"
        )
        deadline = ExecutionDeadline(timeout)
        token = active_execution_deadline.set(deadline)
        try:
            execution = asyncio.create_task(
                backend.execute(
                    code=code,
                    language=language,
                    exec_timeout=timeout,
                    work_dir=str(work_dir),
                    on_output=on_output,
                    agent_id=agent_id,
                )
            )
        except BaseException:
            slots.release()
            raise
        finally:
            active_execution_deadline.reset(token)
        # The slot now belongs to the backend task and frees only when that task really ends, even after this call
        # abandons it past the hard deadline.
        execution.add_done_callback(lambda _: slots.release())
        try:
            done, _ = await asyncio.wait((execution,), timeout=deadline.settle_remaining())
            if done:
                result = execution.result()
            else:
                _ = execution.cancel()
                done, _ = await asyncio.wait((execution,), timeout=deadline.hard_remaining())
                if done:
                    _ = await asyncio.gather(execution, return_exceptions=True)
                else:
                    # A backend that suppresses cancellation cannot be forcibly stopped by asyncio. Observe its eventual
                    # result without letting its teardown extend the tool deadline.
                    execution.add_done_callback(_observe_execution)
                result = ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=1,
                    duration_ms=deadline.elapsed_ms(),
                    error="Sandbox backend exceeded its deadline" + ("; cleanup incomplete" if not done else ""),
                    termination=TerminationReason.BACKEND_ABORT,
                    output_dropped=on_output is not None,
                )
        except asyncio.CancelledError:
            _ = execution.cancel()
            done, _ = await asyncio.wait((execution,), timeout=deadline.hard_remaining())
            if done:
                _ = await asyncio.gather(execution, return_exceptions=True)
            else:
                execution.add_done_callback(_observe_execution)
            raise
        return backend._format_result(result)

    except ValueError as e:
        if is_e2b_tool:
            return f"❌ E2B sandbox configuration error: {str(e)[:300]}\nPlease check the API key in the tool settings."
        return f"❌ Sandbox configuration error: {str(e)[:300]}"

    except Exception as e:
        logger.exception(f"[Sandbox] Execution failed for agent {agent_id} (tool={tool_name})")
        if is_e2b_tool:
            return f"❌ E2B execution error: {str(e)[:200]}"
        return f"❌ Execution error: {str(e)[:200]}"


def _observe_execution(execution: asyncio.Task[ExecutionResult]) -> None:
    if not execution.cancelled():
        _ = execution.exception()
