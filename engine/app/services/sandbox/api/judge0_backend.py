"""Judge0 API-based sandbox backend."""

import asyncio
from typing import override

import anyio
import httpx

from app.core.json_types import json_as_int, json_as_str, json_as_str_or, json_object_from, json_object_from_response
from app.core.logging import logger
from app.core.outbound_policy import OutboundPolicyError
from app.services.sandbox.base import (
    BaseSandboxBackend,
    ExecutionDeadline,
    ExecutionResult,
    SandboxCapabilities,
    TerminationReason,
    active_execution_deadline,
)
from app.services.sandbox.config import ENDPOINT_BLOCKED_MESSAGE, SandboxConfig

# Judge0 language IDs
_JUDGE0_LANGUAGE_IDS = {
    "python": 71,  # Python 3
    "python3": 71,
    "bash": 28,  # Bash
    "sh": 28,
    "node": 63,  # JavaScript (Node.js)
    "javascript": 63,
}

# Default Judge0 API URL
_DEFAULT_JUDGE0_URL = "https://api.judge0.com"


class Judge0Backend(BaseSandboxBackend):
    """Judge0 API-based sandbox backend.

    Judge0 (https://judge0.com/) is an open-source online judge system
    that provides a REST API for code execution.
    """

    @property
    @override
    def name(self) -> str:
        return "judge0"

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config
        self.api_url: str = config.api_url or _DEFAULT_JUDGE0_URL

        if not config.api_key:
            # Judge0 has a free tier that doesn't require API key
            # But for production, you should set one
            pass

    @override
    def get_capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            supported_languages=["python", "python3", "bash", "node", "javascript"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=256,
            network_available=False,
            filesystem_available=False,
        )

    @override
    async def health_check(self) -> bool:
        """Check if Judge0 API is available."""
        try:
            async with self.config.http_client(base_url=self.api_url, timeout=5.0) as client:
                response = await client.get(f"{self.api_url}/languages", timeout=5.0)
                return response.status_code == 200
        except OutboundPolicyError:
            logger.warning("[Judge0] Health check blocked by outbound network policy")
            return False
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
        """Execute code using Judge0 API."""
        if "timeout" in kwargs:
            raise TypeError("execute() no longer accepts 'timeout'; use 'exec_timeout'")
        timeout = exec_timeout
        supplied = kwargs.get("deadline")
        deadline = (
            supplied
            if isinstance(supplied, ExecutionDeadline)
            else active_execution_deadline.get() or ExecutionDeadline(timeout)
        )

        # Get language ID
        lang_id = _JUDGE0_LANGUAGE_IDS.get(language.lower())
        if lang_id is None:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error=f"Unsupported language: {language}. Supported: {', '.join(_JUDGE0_LANGUAGE_IDS.keys())}",
            )

        try:
            async with (
                asyncio.timeout(deadline.remaining()),
                self.config.http_client(base_url=self.api_url, timeout=deadline.remaining()) as client,
            ):
                # Step 1: Submit the code
                submit_response = await client.post(
                    f"{self.api_url}/submissions",
                    json={
                        "source_code": code,
                        "language_id": lang_id,
                        "cpu_time_limit": timeout,
                        "memory_limit": self.config.memory_limit,
                    },
                    timeout=deadline.remaining(),
                )

                if submit_response.status_code != 201:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=deadline.elapsed_ms(),
                        error=f"Failed to submit code: {submit_response.status_code}",
                        termination=TerminationReason.BACKEND_ABORT,
                    )

                submission = json_object_from_response(submit_response)
                token = json_as_str(submission.get("token"))

                if token in (None, ""):
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=deadline.elapsed_ms(),
                        error="Failed to get submission token",
                        termination=TerminationReason.BACKEND_ABORT,
                    )

                # Step 2: Poll for result
                while not deadline.expired:
                    result_response = await client.get(
                        f"{self.api_url}/submissions/{token}",
                        params={"fields": "stdout,stderr,status,time,memory,exit_code"},
                        timeout=deadline.remaining(),
                    )

                    result_response.raise_for_status()

                    if result_response.status_code == 200:
                        result = json_object_from_response(result_response)

                        status = json_object_from(result.get("status"))
                        status_id_raw: object = status.get("id")
                        if not isinstance(status_id_raw, int) or isinstance(status_id_raw, bool):
                            await anyio.sleep(min(0.5, deadline.remaining()))
                            continue

                        # Check if still processing
                        if status_id_raw <= 2:  # In Queue or Processing
                            await anyio.sleep(min(0.5, deadline.remaining()))
                            continue

                        # Completed
                        duration_ms = deadline.elapsed_ms()
                        stdout = json_as_str_or(result.get("stdout"))
                        stderr = json_as_str_or(result.get("stderr"))
                        exit_code = json_as_int(result.get("exit_code"), 1)

                        # Truncate output
                        stdout = stdout[:10000]
                        stderr = stderr[:5000]

                        return ExecutionResult(
                            success=status_id_raw == 3,  # Accepted
                            stdout=stdout,
                            stderr=stderr,
                            exit_code=exit_code,
                            duration_ms=duration_ms,
                            error=(
                                None
                                if status_id_raw == 3
                                else json_as_str_or(status.get("description"), "Execution failed")
                            ),
                            termination=(
                                TerminationReason.PROVIDER_TIME_LIMIT
                                if status_id_raw == 5
                                else TerminationReason.COMPLETED
                            ),
                        )

                    await anyio.sleep(min(0.5, deadline.remaining()))

                # Timeout waiting for result
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=124,
                    duration_ms=deadline.elapsed_ms(),
                    error=f"Code execution timed out after {timeout}s",
                    termination=TerminationReason.SANDBOX_TIMEOUT,
                )

        except TimeoutError:
            duration_ms = deadline.elapsed_ms()
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=124,
                duration_ms=duration_ms,
                error=f"Code execution timed out after {timeout}s",
                termination=TerminationReason.SANDBOX_TIMEOUT,
            )

        except httpx.HTTPError:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error="Judge0 HTTP transport or response failure",
                termination=TerminationReason.BACKEND_ABORT,
            )

        except OutboundPolicyError:
            logger.warning("[Judge0] Execution blocked by outbound network policy")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error=ENDPOINT_BLOCKED_MESSAGE,
                termination=TerminationReason.BACKEND_ABORT,
            )

        except Exception as e:
            duration_ms = deadline.elapsed_ms()
            logger.exception("[Judge0] Execution error")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"Judge0 execution error: {str(e)[:200]}",
                termination=TerminationReason.BACKEND_ABORT,
            )
