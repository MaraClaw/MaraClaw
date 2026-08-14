"""E2B API-based sandbox backend."""

import importlib
import time
from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager
from types import ModuleType
from typing import Protocol, TypeGuard, override

from app.core.logging import logger
from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig


class E2bCommandResult(Protocol):
    exit_code: int | None
    stdout: str | None
    stderr: str | None


class E2bCommands(Protocol):
    async def run(self, command: str) -> E2bCommandResult: ...


class E2bSandbox(Protocol):
    commands: E2bCommands


class E2bAsyncSandbox(Protocol):
    async def list(self, *, api_key: str) -> object: ...

    def create(
        self, *, api_key: str, timeout: int
    ) -> Coroutine[object, object, AbstractAsyncContextManager[E2bSandbox]]: ...


class E2bModule(Protocol):
    AsyncSandbox: E2bAsyncSandbox


def _has_async_sandbox(module: ModuleType) -> TypeGuard[E2bModule]:
    return hasattr(module, "AsyncSandbox")


# Lazy import e2b to make it optional
_e2b: E2bModule | None = None


def _get_e2b() -> E2bModule:
    """Lazy load e2b SDK."""
    global _e2b
    if _e2b is None:
        try:
            module = importlib.import_module("e2b")
            if not _has_async_sandbox(module):
                raise ImportError("e2b package does not provide AsyncSandbox")
            _e2b = module
        except ImportError as exc:
            raise ImportError("e2b package is required for E2B backend. Install it with: pip install e2b") from exc
    return _e2b


# Language mapping for E2B
_LANGUAGE_MAP = {
    "python": "python",
    "bash": "bash",
    "node": "node",
    "javascript": "javascript",
}


class E2bBackend(BaseSandboxBackend):
    """E2B cloud-based sandbox backend.

    E2B (https://e2b.dev/) provides secure, cloud-based code execution
    with built-in isolation and networking.
    """

    @property
    @override
    def name(self) -> str:
        return "e2b"

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config
        self._client: E2bAsyncSandbox | None = None

        if not config.api_key:
            raise ValueError("E2B API key is required. Set SANDBOX_API_KEY environment variable.")

    @property
    def client(self) -> E2bAsyncSandbox:
        """Get or create E2B client."""
        e2b_lib = _get_e2b()
        if self._client is None:
            self._client = e2b_lib.AsyncSandbox
        return self._client

    @override
    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node", "javascript"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=512,
            network_available=True,
            filesystem_available=True,
        )

    @override
    async def health_check(self) -> bool:
        """Check if E2B service is available."""
        try:
            e2b_lib = _get_e2b()
            # Try to list sandboxes to verify API is accessible
            _ = await e2b_lib.AsyncSandbox.list(api_key=self.config.api_key)
            return True
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
        """Execute code using E2B cloud sandbox."""
        timeout = exec_timeout
        start_time = time.time()

        # Map language to E2B format
        e2b_language = _LANGUAGE_MAP.get(language, language)
        if language not in _LANGUAGE_MAP:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}",
            )

        e2b_lib = _get_e2b()

        try:
            # Create sandbox and run code
            async with await e2b_lib.AsyncSandbox.create(
                api_key=self.config.api_key,
                timeout=timeout,
            ) as sandbox:
                # Build the command based on language
                if e2b_language == "python":
                    cmd = "python3"
                    args = ["-c", code]
                elif e2b_language == "bash":
                    cmd = "bash"
                    args = ["-c", code]
                elif e2b_language in ("node", "javascript"):
                    cmd = "node"
                    args = ["-e", code]
                else:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error=f"Unsupported language: {language}",
                    )

                # Run the command - use string format for e2b
                cmd_str = f"{cmd} {args[0]} '{args[1]}'"
                result = await sandbox.commands.run(cmd_str)

            duration_ms = int((time.time() - start_time) * 1000)

            return ExecutionResult(
                success=result.exit_code == 0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exit_code=result.exit_code or 0,
                duration_ms=duration_ms,
                error=None,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = str(e)
            logger.exception("[E2B] Execution error")

            # Handle timeout
            if "timeout" in error_msg.lower():
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=124,
                    duration_ms=duration_ms,
                    error=f"Code execution timed out after {timeout}s",
                )

            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"E2B execution error: {error_msg[:200]}",
            )
