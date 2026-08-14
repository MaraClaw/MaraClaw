"""Local docker-based sandbox backend."""

import time
from collections.abc import Iterable
from typing import Any, override

from anyio import fail_after
from anyio.to_thread import run_sync

from app.core.logging import logger
from app.services.sandbox.base import BaseSandboxBackend, ExecutionResult, SandboxCapabilities
from app.services.sandbox.config import SandboxConfig

# Lazy import python-on-whales to make it optional
_docker_client_cls = None


def _get_docker():
    """Lazy load python-on-whales Docker client."""
    global _docker_client_cls
    if _docker_client_cls is None:
        try:
            from python_on_whales import DockerClient

            _docker_client_cls = DockerClient
        except ImportError as exc:
            raise ImportError(
                "python-on-whales package is required for docker backend. Install it with: uv add python-on-whales"
            ) from exc
    return _docker_client_cls


# Language to docker image mapping
_DOCKER_IMAGES = {
    "python": "python:3.14.6-slim",
    "bash": "bash:5.3",
    "node": "node:26.5.0-slim",
}

# Docker run command mapping
_DOCKER_COMMANDS = {
    "python": ["python3", "-c"],
    "bash": ["bash", "-c"],
    "node": ["node", "-e"],
}


class DockerBackend(BaseSandboxBackend):
    """Docker-based sandbox backend.

    This backend executes code inside Docker containers for better isolation.
    It requires python-on-whales to be installed and docker daemon to be running.
    """

    @property
    @override
    def name(self) -> str:
        return "docker"

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config
        self._client: Any = None

    @property
    def client(self):
        """Lazy load docker client."""
        if self._client is None:
            docker_client_cls = _get_docker()
            self._client = docker_client_cls()
        return self._client

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
        """Check if docker is available and running."""
        try:
            _ = self.client.info()
            return True
        except Exception:
            return False

    @override
    async def execute(
        self,
        code: str,
        language: str,
        timeout: int = 30,
        work_dir: str | None = None,
        **kwargs: Any,
    ) -> ExecutionResult:
        """Execute code inside a docker container."""
        start_time = time.time()

        # Validate language
        if language not in _DOCKER_IMAGES:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=int((time.time() - start_time) * 1000),
                error=f"Unsupported language: {language}. Use: {', '.join(_DOCKER_IMAGES.keys())}",
            )

        # Get image and command
        image = _DOCKER_IMAGES[language]

        # Prepare environment (proxy only when allow_network; config-only)
        env = {
            "HOME": "/root",
            "PYTHONDONTWRITEBYTECODE": "1",
            **self.config.resolve_proxy_env(),
        }

        cmd = [*_DOCKER_COMMANDS[language], code]

        # Resource limits
        cpu_limit = self.config.cpu_limit
        memory_limit = self.config.memory_limit

        # Network config
        networks = ["bridge"] if self.config.allow_network else ["none"]
        container = None

        try:
            # Pull image if needed
            if not self.client.image.exists(image):
                _ = self.client.image.pull(image)

            # Run container
            container = self.client.run(
                image,
                cmd,
                detach=True,
                memory=memory_limit,
                cpu_period=100000,  # Docker default
                cpu_quota=int(float(cpu_limit) * 100000),
                networks=networks,
                envs=env,
            )

            # Wait for container with timeout
            try:
                with fail_after(timeout):
                    result = await run_sync(
                        self.client.container.wait,
                        container,
                        abandon_on_cancel=True,
                    )
            except TimeoutError:
                return ExecutionResult(
                    success=False,
                    stdout="",
                    stderr="",
                    exit_code=124,
                    duration_ms=int((time.time() - start_time) * 1000),
                    error=f"Code execution timed out after {timeout}s",
                )

            # Get output
            stdout, stderr = _collect_logs(self.client.container.logs(container, stream=True))

            duration_ms = int((time.time() - start_time) * 1000)
            exit_code = _normalize_exit_code(result)

            return ExecutionResult(
                success=exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=duration_ms,
                error=None if exit_code == 0 else f"Exit code: {exit_code}",
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = str(e)
            logger.exception("[Docker] Execution error")

            # Handle timeout specifically
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
                error=f"Docker execution error: {error_msg[:200]}",
            )
        finally:
            if container is not None:
                try:
                    self.client.container.remove(container, force=True)
                except Exception as cleanup_error:
                    logger.warning(f"[Docker] Failed to remove container after execution: {cleanup_error}")


def _decode_log(log: bytes | str) -> str:
    if isinstance(log, bytes):
        return log.decode("utf-8", errors="replace")
    return log


def _collect_logs(logs: str | Iterable[object]) -> tuple[str, str]:
    if isinstance(logs, str):
        return logs[:10000], ""
    stdout_parts = []
    stderr_parts = []
    for item in logs:
        if isinstance(item, (bytes, str)):
            stdout_parts.append(_decode_log(item))
            continue
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        stream_name: Any
        content: Any
        stream_name, content = item
        if stream_name == "stdout" and isinstance(content, (bytes, str)):
            stdout_parts.append(_decode_log(content))
        elif stream_name == "stderr" and isinstance(content, (bytes, str)):
            stderr_parts.append(_decode_log(content))
    return "".join(stdout_parts)[:10000], "".join(stderr_parts)[:5000]


def _normalize_exit_code(result: int | dict[str, int]) -> int:
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        return int(result.get("StatusCode", 1))
    return 1
