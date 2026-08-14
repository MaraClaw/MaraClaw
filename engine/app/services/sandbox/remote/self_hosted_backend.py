"""Self-hosted sandbox backend."""

import time
from typing import override

import httpx

from app.core.json_types import (
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


class SelfHostedBackend(BaseSandboxBackend):
    """Self-hosted sandbox backend.

    Connects to user-deployed sandbox services like aio-sandbox.

    Usage:
    - Set SANDBOX_API_URL to full endpoint URL
    - For aio-sandbox shell: http://localhost:8080/v1/shell/exec
    - For aio-sandbox jupyter: http://localhost:8080/v1/jupyter/execute

    Response format expected: {"success": bool, "output": str, "error": str?}
    """

    @property
    @override
    def name(self) -> str:
        return "self_hosted"

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config

        if not config.api_url:
            raise ValueError("Self-hosted sandbox URL is required. Set SANDBOX_API_URL environment variable.")

        # Normalize URL (remove trailing slash)
        self.api_url: str = config.api_url.rstrip("/")

    @override
    def get_capabilities(self) -> SandboxCapabilities:
        # Capabilities depend on the self-hosted service
        # We'll report conservative defaults
        return SandboxCapabilities(
            supported_languages=["python", "bash", "node", "javascript"],
            max_timeout=self.config.max_timeout,
            max_memory_mb=256,
            network_available=True,
            filesystem_available=True,
        )

    @override
    async def health_check(self) -> bool:
        """Check if the self-hosted service is available."""
        try:
            async with httpx.AsyncClient() as client:
                # Try /v1/sandbox first (aio-sandbox), then fall back to /health
                for endpoint in ["/v1/sandbox", "/health"]:
                    check_url = (
                        self.api_url.split("/v1/")[0] + endpoint
                        if "/v1/" in self.api_url
                        else f"{self.api_url.rsplit('/', 1)[0]}/health"
                    )
                    try:
                        response = await client.get(check_url, timeout=5.0)
                        if response.status_code == 200:
                            return True
                    except httpx.HTTPError as exc:
                        logger.debug(
                            "Self-hosted sandbox health probe failed for {}: {}", check_url, type(exc).__name__
                        )
                        continue
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
        """Execute code using the self-hosted sandbox service."""
        timeout = resolve_exec_timeout(exec_timeout, kwargs)
        start_time = time.time()

        # Build request
        headers = {
            "Content-Type": "application/json",
        }

        # Add API key if configured
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        # Build payload based on the API endpoint
        # For shell exec: {"cmd": "..."}
        # For jupyter: {"code": "..."}
        payload = {}

        # Detect endpoint type from URL and build appropriate payload
        url_lower = self.api_url.lower()
        if "shell" in url_lower:
            # aio-sandbox shell: wrap code as command
            if language == "python":
                cmd = f"python3 -c {code!r}"
            elif language == "bash":
                cmd = code
            elif language == "node":
                cmd = f"node -e {code!r}"
            else:
                cmd = code
            payload: dict[str, object] = {"cmd": cmd}
        elif "jupyter" in url_lower:
            # aio-sandbox jupyter
            payload = {"code": code}
        else:
            # Generic format
            payload = {
                "code": code,
                "language": language,
                "timeout": timeout,
            }

        # Add any additional kwargs
        payload.update(kwargs)

        try:
            async with httpx.AsyncClient() as client:
                # Use URL directly without appending /execute
                response = await client.post(
                    self.api_url,
                    json=payload,
                    headers=headers,
                    timeout=float(timeout + 10),  # Add buffer for network
                )

                duration_ms = int((time.time() - start_time) * 1000)

                if response.status_code != 200:
                    return ExecutionResult(
                        success=False,
                        stdout="",
                        stderr="",
                        exit_code=response.status_code,
                        duration_ms=duration_ms,
                        error=f"Sandbox service error: HTTP {response.status_code} - {response.text[:200]}",
                    )

                result = json_object_from_response(response)

                # Parse response - support multiple formats:
                # Generic: {"success": true, "stdout": "...", "stderr": "...", "exit_code": 0}
                # aio-sandbox shell: {"success": true, "data": {"output": "..."}}
                # aio-sandbox jupyter: {"output": "...", "status": "ok"}

                # Try to extract output
                output = ""
                stderr = ""
                success = True
                error_msg = None
                exit_code = 0

                data_raw: object = result.get("data")
                # aio-sandbox shell format
                if "data" in result and isinstance(data_raw, dict):
                    output = json_as_str_or(json_object_from(data_raw).get("output"))
                # aio-sandbox jupyter format
                elif "output" in result and "status" in result:
                    output = json_as_str_or(result.get("output"))
                    if json_as_str(result.get("status")) != "ok":
                        success = False
                        error_msg = json_as_str(result.get("error")) or json_as_str(result.get("output")) or ""
                # Generic format
                else:
                    output = json_as_str_or(result.get("stdout")) or json_as_str_or(result.get("output"))
                    stderr = json_as_str_or(result.get("stderr"))
                    success = json_as_bool(result.get("success"), True)
                    exit_code = json_as_int(result.get("exit_code"), 0 if success else 1)
                    error_msg = json_as_str(result.get("error"))

                return ExecutionResult(
                    success=success,
                    stdout=output[:10000],
                    stderr=stderr[:5000],
                    exit_code=exit_code,
                    duration_ms=json_as_int(result.get("duration_ms"), duration_ms),
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
            logger.exception("[SelfHosted] Execution error")
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=duration_ms,
                error=f"Self-hosted sandbox error: {str(e)[:200]}",
            )
