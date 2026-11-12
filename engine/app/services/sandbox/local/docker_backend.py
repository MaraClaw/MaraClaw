"""Local docker-based sandbox backend."""

import asyncio
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from functools import partial
from typing import Protocol, TypeIs, override

from anyio import fail_after
from anyio.to_thread import run_sync

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


class DockerImageApi(Protocol):
    def exists(self, image: str) -> bool: ...

    def pull(self, image: str) -> object: ...


class DockerLogStream(Protocol):
    def __iter__(self) -> Iterator[tuple[str, bytes]]: ...

    def close(self) -> None: ...


class DockerContainerApi(Protocol):
    def wait(self, container: object) -> object: ...

    def logs(self, container: object, stream: bool = False) -> DockerLogStream: ...

    def remove(self, container: object, force: bool = True) -> None: ...


class DockerClientLike(Protocol):
    image: DockerImageApi
    container: DockerContainerApi

    def info(self) -> object: ...

    def run(
        self,
        image: str,
        command: list[str],
        *,
        detach: bool,
        memory: str,
        cpu_period: int,
        cpu_quota: int,
        networks: list[str],
        envs: dict[str, str],
    ) -> object: ...


class DockerClientFactory(Protocol):
    def __call__(self) -> DockerClientLike: ...


def _is_docker_factory(value: object) -> TypeIs[DockerClientFactory]:
    return callable(value)


# Lazy import pywhales to make it optional
_docker_client_cls: DockerClientFactory | None = None


def _get_docker() -> DockerClientFactory:
    """Lazy load pywhales Docker client."""
    global _docker_client_cls
    if _docker_client_cls is None:
        try:
            from pywhales import DockerClient
        except ImportError as exc:
            raise ImportError(
                "pywhales package is required for docker backend. Install it with: uv add pywhales"
            ) from exc
        if not _is_docker_factory(DockerClient):
            raise ImportError("pywhales DockerClient is not callable")
        _docker_client_cls = DockerClient
    return _docker_client_cls


# Language to docker image mapping
_DOCKER_IMAGES = {
    "python": "python:3.14.10-slim-trixie",
    "bash": "bash:5.3",
    "node": "node:26.13.1-slim",
}

# Docker run command mapping
_DOCKER_COMMANDS = {
    "python": ["python3", "-c"],
    "bash": ["bash", "-c"],
    "node": ["node", "-e"],
}

# Removals outlive the caller that started them, so the loop must not collect them mid-flight.
_removals: set[asyncio.Task[None]] = set()


class _ContainerSlot:
    """Hand-off of a container created in a worker thread: exactly one side ends up removing it."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._abandoned: bool = False
        self._container: object | None = None

    def publish(self, container: object) -> bool:
        """Worker thread: give the new container to the loop. False when the loop already gave up on it."""
        with self._lock:
            if self._abandoned:
                return False
            self._container = container
            return True

    def abandon(self) -> object | None:
        """Event loop: stop accepting a container. Returns the one already published, if any."""
        with self._lock:
            self._abandoned = True
            return self._container


def _timeout_result(timeout: int, deadline: ExecutionDeadline) -> ExecutionResult:
    return ExecutionResult(
        success=False,
        stdout="",
        stderr="",
        exit_code=124,
        duration_ms=deadline.elapsed_ms(),
        error=f"Code execution timed out after {timeout}s",
        termination=TerminationReason.SANDBOX_TIMEOUT,
    )


class DockerBackend(BaseSandboxBackend):
    """Docker-based sandbox backend.

    This backend executes code inside Docker containers for better isolation.
    It requires pywhales to be installed and docker daemon to be running.
    """

    @property
    @override
    def name(self) -> str:
        return "docker"

    def __init__(self, config: SandboxConfig):
        self.config: SandboxConfig = config
        self._client: DockerClientLike | None = None

    @property
    def client(self) -> DockerClientLike:
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
            _ = await run_sync(lambda: self.client.info(), abandon_on_cancel=True)
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
        """Execute code inside a docker container."""
        if "timeout" in kwargs:
            raise TypeError("execute() no longer accepts 'timeout'; use 'exec_timeout'")
        timeout = exec_timeout
        supplied = kwargs.get("deadline")
        deadline = (
            supplied
            if isinstance(supplied, ExecutionDeadline)
            else active_execution_deadline.get() or ExecutionDeadline(exec_timeout)
        )

        # Validate language
        if language not in _DOCKER_IMAGES:
            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
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
        slot = _ContainerSlot()

        try:
            # One deadline covers client load, image check, pull, creation, wait and log collection
            with fail_after(deadline.remaining()):
                client = await run_sync(lambda: self.client, abandon_on_cancel=True)

                # Pull image if needed
                if not await run_sync(client.image.exists, image, abandon_on_cancel=True):
                    _ = await run_sync(client.image.pull, image, abandon_on_cancel=True)

                # Run container
                container = await run_sync(
                    self._create,
                    slot,
                    partial(
                        client.run,
                        image,
                        cmd,
                        detach=True,
                        memory=memory_limit,
                        cpu_period=100000,  # Docker default
                        cpu_quota=int(float(cpu_limit) * 100000),
                        networks=networks,
                        envs=env,
                    ),
                    abandon_on_cancel=True,
                )

                result = await run_sync(client.container.wait, container, abandon_on_cancel=True)
                stdout, stderr = await run_sync(self._collect_logs, container, abandon_on_cancel=True)

            exit_code = _normalize_exit_code(result)

            return ExecutionResult(
                success=exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_ms=deadline.elapsed_ms(),
                error=None if exit_code == 0 else f"Exit code: {exit_code}",
            )

        except TimeoutError:
            return _timeout_result(timeout, deadline)

        except Exception as e:
            error_msg = str(e)
            logger.exception("[Docker] Execution error")

            # Handle timeout specifically
            if "timeout" in error_msg.lower():
                return _timeout_result(timeout, deadline)

            return ExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=1,
                duration_ms=deadline.elapsed_ms(),
                error=f"Docker execution error: {error_msg[:200]}",
            )
        finally:
            await self._remove(slot, deadline)

    def _create(self, slot: _ContainerSlot, create: Callable[[], object]) -> object:
        container = create()
        if not slot.publish(container):
            self._remove_container(container)
        return container

    async def _remove(self, slot: _ContainerSlot, deadline: ExecutionDeadline) -> None:
        container = slot.abandon()
        if container is None:
            return
        # A task of its own: no cancellation of the caller, however it is delivered, can stop the removal from starting
        removal = asyncio.create_task(run_sync(self._remove_container, container))
        _removals.add(removal)
        removal.add_done_callback(_removals.discard)
        _ = await asyncio.wait((removal,), timeout=deadline.settle_remaining())

    def _remove_container(self, container: object) -> None:
        try:
            self.client.container.remove(container, force=True)
        except Exception as cleanup_error:
            logger.warning(f"[Docker] Failed to remove container after execution: {cleanup_error}")

    def _collect_logs(self, container: object) -> tuple[str, str]:
        with closing(self.client.container.logs(container, stream=True)) as logs:
            return _collect_logs(logs, self.config)


def _collect_logs(logs: Iterable[tuple[str, bytes]], config: SandboxConfig) -> tuple[str, str]:
    stdout = bytearray()
    stderr = bytearray()
    for stream_name, content in logs:
        if stream_name == "stdout":
            target, limit = stdout, config.max_stdout_bytes
        elif stream_name == "stderr":
            target, limit = stderr, config.max_stderr_bytes
        else:
            continue
        remaining = limit - len(target)
        if remaining > 0:
            target.extend(content[:remaining])
    return stdout.decode("utf-8", errors="replace")[:10000], stderr.decode("utf-8", errors="replace")[:5000]


def _normalize_exit_code(result: object) -> int:
    if isinstance(result, int) and not isinstance(result, bool):
        return result
    if isinstance(result, dict):
        status = result.get("StatusCode", 1)
        return status if isinstance(status, int) and not isinstance(status, bool) else 1
    return 1
