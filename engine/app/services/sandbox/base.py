"""Sandbox backend interface definitions."""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Fixed allowance past the execution timeout for killing, draining and reporting. Every execution path returns within
# ``timeout + CLEANUP_GRACE_SECONDS`` of its single monotonic deadline.
CLEANUP_GRACE_SECONDS: float = 5.0


class TerminationReason(StrEnum):
    """Why an execution ended, independent of the code's own exit status."""

    COMPLETED = "completed"
    SANDBOX_TIMEOUT = "sandbox_timeout"
    PROVIDER_TIME_LIMIT = "provider_time_limit"
    BACKEND_ABORT = "backend_abort"


_TERMINATION_NOTICES: dict[TerminationReason, str] = {
    TerminationReason.COMPLETED: "Termination: completed",
    TerminationReason.SANDBOX_TIMEOUT: (
        "⏱️ Termination: sandbox timeout - the code ran past the execution time limit and was stopped"
    ),
    TerminationReason.PROVIDER_TIME_LIMIT: (
        "⏱️ Termination: provider time limit - the sandbox provider stopped the code at its own time limit"
    ),
    TerminationReason.BACKEND_ABORT: (
        "🛑 Termination: backend abort - the sandbox failed or was aborted; this is not a code timeout"
    ),
}
_OUTPUT_DROPPED_NOTICE = (
    "✂️ Streamed output was dropped because the live stream stalled or failed; the output above is the captured result"
)


class ExecutionDeadline:
    """One monotonic deadline for an execution: ``timeout`` for the code, then a fixed grace for cleanup.

    ``settle_at`` is where a backend must have finished its own teardown. The second half of the grace belongs to the
    outer guard that cancels a backend which did not settle, so the two never compete for the same instant.
    """

    def __init__(
        self, timeout: float, *, grace: float | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        grace_seconds = CLEANUP_GRACE_SECONDS if grace is None else grace
        self._clock: Callable[[], float] = clock
        self.started_at: float = clock()
        self.expires_at: float = self.started_at + max(0.0, timeout)
        self.settle_at: float = self.expires_at + grace_seconds / 2
        self.hard_at: float = self.expires_at + grace_seconds

    def remaining(self) -> float:
        """Seconds left for the code itself."""
        return max(0.0, self.expires_at - self._clock())

    def settle_remaining(self) -> float:
        """Seconds left before a backend must have finished its own teardown."""
        return max(0.0, self.settle_at - self._clock())

    def hard_remaining(self) -> float:
        """Seconds left before the whole call must have returned."""
        return max(0.0, self.hard_at - self._clock())

    @property
    def expired(self) -> bool:
        return self._clock() >= self.expires_at

    def elapsed_ms(self) -> int:
        return int((self._clock() - self.started_at) * 1000)

    def step_budget(self, cap: float) -> float:
        """Budget for one teardown step: at most ``cap`` and at most half of what is left before ``settle_at``."""
        return min(cap, self.settle_remaining() / 2)


# Task-local metadata must never enter backend kwargs: some remote adapters serialize those into their HTTP payload.
active_execution_deadline: ContextVar[ExecutionDeadline | None] = ContextVar("sandbox_execution_deadline", default=None)


@dataclass
class ExecutionResult:
    """Result of code execution in a sandbox."""

    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    error: str | None = None
    termination: TerminationReason = TerminationReason.COMPLETED
    output_dropped: bool = False


@dataclass
class SandboxCapabilities:
    """Capabilities of a sandbox backend."""

    supported_languages: list[str]
    max_timeout: int
    max_memory_mb: int
    network_available: bool
    filesystem_available: bool


@runtime_checkable
class SandboxBackend(Protocol):
    """Protocol defining the interface for sandbox backends."""

    @property
    def name(self) -> str:
        """Backend name for identification."""
        ...

    async def execute(
        self,
        code: str,
        language: str,
        exec_timeout: int = 30,
        work_dir: str | None = None,
        **kwargs: object,
    ) -> ExecutionResult:
        """
        Execute code in the sandbox.

        Args:
            code: The code to execute
            language: Programming language (python, bash, node, etc.)
            exec_timeout: Execution timeout in seconds
            work_dir: Working directory for execution (optional)
            **kwargs: Additional backend-specific options. ``deadline`` may carry the caller's ExecutionDeadline;
                backends that honor it must return before ``deadline.settle_at``.

        Returns:
            ExecutionResult with execution details
        """
        ...

    async def health_check(self) -> bool:
        """
        Check if the sandbox backend is healthy and available.

        Returns:
            True if the backend is healthy, False otherwise
        """
        ...

    def get_capabilities(self) -> SandboxCapabilities:
        """
        Get the capabilities of this sandbox backend.

        Returns:
            SandboxCapabilities describing what this backend supports
        """
        ...

    def _format_result(self, result: ExecutionResult) -> str:
        """Format an execution result for user display."""
        ...


class BaseSandboxBackend(ABC):
    """Base class providing common functionality for sandbox backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name for identification."""

    @abstractmethod
    async def execute(
        self,
        code: str,
        language: str,
        exec_timeout: int = 30,
        work_dir: str | None = None,
        **kwargs: object,
    ) -> ExecutionResult:
        """Execute code in the sandbox."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the sandbox backend is healthy."""

    @abstractmethod
    def get_capabilities(self) -> SandboxCapabilities:
        """Get the capabilities of this sandbox backend."""

    def _format_result(self, result: ExecutionResult) -> str:
        """Format execution result for user display."""
        result_parts = []

        if result.stdout.strip():
            result_parts.append(f"📤 Output:\n{result.stdout}")
        if result.stderr.strip():
            result_parts.append(f"⚠️ Stderr:\n{result.stderr}")
        if result.error not in (None, ""):
            result_parts.append(f"❌ Error: {result.error}")
        if result.exit_code != 0 and result.error in (None, ""):
            result_parts.append(f"Exit code: {result.exit_code}")

        notices: list[str] = []
        if result.termination in _TERMINATION_NOTICES:
            notices.append(_TERMINATION_NOTICES[result.termination])
        notices.append(_OUTPUT_DROPPED_NOTICE if result.output_dropped else "Streamed output dropped: no")

        if not result_parts and result.termination == TerminationReason.COMPLETED:
            result_parts.append("✅ Code executed successfully (no output)")

        return "\n\n".join([*result_parts, *notices])
