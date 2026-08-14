"""aio-sandbox backend."""

import time
from typing import override

import httpx

from app.core.json_types import (
    is_any_list,
    json_as_bool,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_object_from,
    json_object_from_response,
)
from app.core.logging import logger
from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities, resolve_exec_timeout
from app.services.sandbox.config import SandboxConfig


class AioSandboxBackend(BaseSandboxBackend):
    """aio-sandbox backend.

    Connects to aio-sandbox (https://github.com/agent-infra/sandbox).

    Supports:
    - Shell execution (/v1/shell/exec): bash, node
    - Jupyter execution (/v1/jupyter/execute): python

    Configuration:
    - SANDBOX_API_URL: Base URL of aio-sandbox (e.g., http://localhost:8080)
    - SANDBOX_API_TYPE: Execution type - "shell" or "jupyter" (default: shell)
    - SANDBOX_API_KEY: Optional JWT token for authentication
    """

    @property
    @override
    def name(self) -> str:
        return "aio_sandbox"

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config
        self.base_url: object = config.api_url.rstrip("/") if config.api_url else ""

        if not self.base_url:
            raise ValueError(
                "aio-sandbox URL is required. Set SANDBOX_API_URL environment variable (e.g., http://localhost:8080)."
            )

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
        """Check if aio-sandbox service is available."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.base_url}/v1/sandbox", timeout=5.0)
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
        """Execute code using aio-sandbox."""
        timeout = resolve_exec_timeout(exec_timeout, kwargs)
        start_time = time.time()

        # Determine endpoint based on language
        # Use jupyter for python, shell for others
        if language == "python":
            endpoint = f"{self.base_url}/v1/jupyter/execute"
            payload = {"code": code}
        else:
            # Shell execution for bash/node
            endpoint = f"{self.base_url}/v1/shell/exec"

            # Build command based on language
            if language == "bash":
                cmd = code
            elif language == "node" or language == "javascript":
                cmd = f"node -e {code!r}"
            else:
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=1,
                    duration_ms=0,
                    error=f"Unsupported language: {language}. Use python, bash, or node.",
                )

            payload = {"command": cmd}

        # Build headers
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(endpoint, json=payload, headers=headers, timeout=float(timeout + 10))

                duration_ms = int((time.time() - start_time) * 1000)

                if response.status_code != 200:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=response.status_code,
                        duration_ms=duration_ms,
                        error=f"aio-sandbox error: HTTP {response.status_code} - {response.text[:200]}",
                    )

                result = json_object_from_response(response)

                # Parse response
                # Shell: {"success": true, "data": {"output": "...", "exit_code": 0}}
                # Jupyter: {"success": true, "data": {"status": "ok", "outputs": [{"text": "..."}]}}

                stdout = ""
                stderr = ""
                success = json_as_bool(result.get("success"), True)
                error_msg = None
                exit_code = 0

                data = json_object_from(result.get("data"))

                if language == "python":
                    # Jupyter format - outputs is a list
                    if data and "outputs" in data:
                        outputs_raw: object = data.get("outputs")
                        text_parts: list[str] = []
                        for out_item in outputs_raw if is_any_list(outputs_raw) else []:
                            out_raw: object = out_item
                            out = json_object_from(out_raw)
                            output_type = json_as_str(out.get("output_type"))
                            if output_type == "stream" and json_as_str(out.get("name")) == "stdout":
                                text_parts.append(json_as_str_or(out.get("text")))
                            elif output_type == "error":
                                traceback_raw: object = out.get("traceback")
                                if is_any_list(traceback_raw) and traceback_raw:
                                    traceback_items = list[object](traceback_raw)
                                    first = traceback_items[0]
                                    stderr += first if isinstance(first, str) else ""
                                else:
                                    stderr += json_as_str_or(out.get("evalue"))
                        stdout = "".join(text_parts)

                    if json_as_str(data.get("status")) != "ok":
                        success = False
                        exit_code = 1
                else:
                    # Shell format
                    if data:
                        stdout = json_as_str_or(data.get("output"))
                        exit_code = json_as_int(data.get("exit_code"), 0)

                    if not success:
                        error_msg = json_as_str(data.get("output")) or "Command failed"

                return ExecutionResult(
                    success=success,
                    stdout=stdout[:10000],
                    stderr=stderr[:5000],
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                    error=error_msg,
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
            logger.exception("[AioSandbox] Execution error")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"aio-sandbox error: {str(e)[:200]}",
            )
