"""Judge0 API-based sandbox backend."""

import time
from typing import override

import anyio
import httpx

from app.core.json_types import json_as_int, json_as_str, json_as_str_or, json_object_from, json_object_from_response
from app.core.logging import logger
from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig

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
        self.api_url: object = config.api_url or _DEFAULT_JUDGE0_URL

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
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.api_url}/languages", timeout=5.0)
                return response.status_code == 200
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
        timeout = exec_timeout
        start_time = time.time()

        # Get language ID
        lang_id = _JUDGE0_LANGUAGE_IDS.get(language.lower())
        if lang_id is None:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}. Supported: {', '.join(_JUDGE0_LANGUAGE_IDS.keys())}",
            )

        try:
            async with httpx.AsyncClient() as client:
                # Step 1: Submit the code
                submit_response = await client.post(
                    f"{self.api_url}/submissions",
                    json={
                        "source_code": code,
                        "language_id": lang_id,
                        "cpu_time_limit": timeout,
                        "memory_limit": self.config.memory_limit,
                    },
                    timeout=10.0,
                )

                if submit_response.status_code != 201:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error=f"Failed to submit code: {submit_response.status_code}",
                    )

                submission = json_object_from_response(submit_response)
                token = json_as_str(submission.get("token"))

                if not token:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=1,
                        duration_ms=int((time.time() - start_time) * 1000),
                        error="Failed to get submission token",
                    )

                # Step 2: Poll for result
                max_retries = timeout * 2  # Poll every 0.5 seconds
                for _ in range(max_retries):
                    _ = await client.get(f"{self.api_url}/submissions/{token}")

                    result_response = await client.get(
                        f"{self.api_url}/submissions/{token}",
                        params={"fields": "stdout,stderr,status,time,memory,exit_code"},
                        timeout=5.0,
                    )

                    if result_response.status_code == 200:
                        result = json_object_from_response(result_response)

                        status = json_object_from(result.get("status"))
                        status_id_raw: object = status.get("id")
                        if not isinstance(status_id_raw, int) or isinstance(status_id_raw, bool):
                            await anyio.sleep(0.5)
                            continue

                        # Check if still processing
                        if status_id_raw <= 2:  # In Queue or Processing
                            await anyio.sleep(0.5)
                            continue

                        # Completed
                        duration_ms = int((time.time() - start_time) * 1000)
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
                        )

                    await anyio.sleep(0.5)

                # Timeout waiting for result
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=124,
                    duration_ms=int((time.time() - start_time) * 1000),
                    error=f"Code execution timed out after {timeout}s",
                )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=124,
                duration_ms=duration_ms,
                error=f"Code execution timed out after {timeout}s",
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.exception("[Judge0] Execution error")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"Judge0 execution error: {str(e)[:200]}",
            )
