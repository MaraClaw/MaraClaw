import asyncio
import threading
import tracemalloc
from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, cast, override

import pytest
from anyio import CancelScope, current_time

from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local import docker_backend as docker_backend_module
from app.services.sandbox.local.docker_backend import DockerBackend


class FakeContainer:
    def __init__(self, container_id: str = "container-123"):
        self.id = container_id


class FakeImageApi:
    def __init__(self, exists_result: bool = True):
        self.exists_result = exists_result
        self.exists_calls = []
        self.pull_calls = []

    def exists(self, image: str) -> bool:
        self.exists_calls.append(image)
        return self.exists_result

    def pull(self, image: str) -> None:
        self.pull_calls.append(image)


class FakeContainerApi:
    def __init__(
        self, wait_result: int = 0, wait_error: Exception | None = None, remove_error: Exception | None = None
    ):
        self.wait_result = wait_result
        self.wait_error = wait_error
        self.remove_error = remove_error
        self.wait_calls = []
        self.logs_calls = []
        self.remove_calls = []

    def wait(self, container: FakeContainer) -> int:
        self.wait_calls.append(container)
        if self.wait_error is not None:
            raise self.wait_error
        return self.wait_result

    def logs(self, container: FakeContainer, stream: bool = False) -> Iterator[tuple[str, bytes]]:
        self.logs_calls.append((container, stream))
        yield from [("stdout", b"stdout text"), ("stderr", b"stderr text")]

    def remove(self, container: FakeContainer, force: bool = True) -> None:
        self.remove_calls.append((container, force))
        if self.remove_error is not None:
            raise self.remove_error


class FakeDockerClient:
    def __init__(
        self,
        image_exists: bool = True,
        wait_result: int = 0,
        wait_error: Exception | None = None,
        remove_error: Exception | None = None,
        run_error: Exception | None = None,
        info_error: Exception | None = None,
    ):
        self.image = FakeImageApi(exists_result=image_exists)
        self.container = FakeContainerApi(wait_result=wait_result, wait_error=wait_error, remove_error=remove_error)
        self.run_error = run_error
        self.info_error = info_error
        self.run_args = None
        self.run_kwargs: dict[str, Any] | None = None
        self.created_container = FakeContainer()

    def info(self):
        if self.info_error is not None:
            raise self.info_error
        return {"ServerVersion": "26.0.0"}

    def run(self, *args, **kwargs) -> FakeContainer:
        self.run_args = args
        self.run_kwargs = kwargs
        if self.run_error is not None:
            raise self.run_error
        return self.created_container


def make_backend(
    client: FakeDockerClient,
    allow_network: bool = True,
    **config_kwargs,
) -> DockerBackend:
    backend = DockerBackend(
        SandboxConfig(allow_network=allow_network, cpu_limit="0.5", memory_limit="256m", **config_kwargs)
    )
    backend._client = cast("Any", client)
    return backend


async def test_execute_runs_python_container_with_pywhales_kwargs():
    # Given
    client = FakeDockerClient(wait_result=0)
    backend = make_backend(client, allow_network=True)

    # When
    result = await backend.execute("print('hello')", "python", exec_timeout=7)

    # Then
    assert result.success is True
    assert result.stdout == "stdout text"
    assert result.stderr == "stderr text"
    assert result.exit_code == 0
    assert result.error is None
    assert client.image.exists_calls == ["python:3.14.8-slim-trixie"]
    assert client.image.pull_calls == []
    assert client.run_args == ("python:3.14.8-slim-trixie", ["python3", "-c", "print('hello')"])
    assert client.run_kwargs == {
        "detach": True,
        "memory": "256m",
        "cpu_period": 100000,
        "cpu_quota": 50000,
        "networks": ["bridge"],
        "envs": {"HOME": "/root", "PYTHONDONTWRITEBYTECODE": "1"},
    }
    assert client.container.wait_calls == [client.created_container]
    assert client.container.logs_calls == [
        (client.created_container, True),
    ]
    assert client.container.remove_calls == [(client.created_container, True)]


async def test_execute_injects_proxy_env_when_network_allowed():
    # Given
    client = FakeDockerClient(wait_result=0)
    backend = make_backend(
        client,
        allow_network=True,
        http_proxy="http://proxy.example:8080",
        https_proxy="http://proxy.example:8443",
        no_proxy="localhost,127.0.0.1",
    )

    # When
    result = await backend.execute("print('hello')", "python", exec_timeout=5)

    # Then
    assert result.success is True
    assert client.run_kwargs is not None
    envs = client.run_kwargs["envs"]
    assert envs["http_proxy"] == "http://proxy.example:8080"
    assert envs["HTTP_PROXY"] == "http://proxy.example:8080"
    assert envs["https_proxy"] == "http://proxy.example:8443"
    assert envs["HTTPS_PROXY"] == "http://proxy.example:8443"
    assert envs["no_proxy"] == "localhost,127.0.0.1"
    assert envs["NO_PROXY"] == "localhost,127.0.0.1"
    assert envs["HOME"] == "/root"


async def test_execute_omits_proxy_env_when_network_disallowed():
    # Given
    client = FakeDockerClient(wait_result=0)
    backend = make_backend(
        client,
        allow_network=False,
        http_proxy="http://proxy.example:8080",
        https_proxy="http://proxy.example:8443",
        no_proxy="localhost",
    )

    # When
    result = await backend.execute("echo hi", "bash", exec_timeout=5)

    # Then
    assert result.success is True
    assert client.run_kwargs is not None
    envs = client.run_kwargs["envs"]
    assert client.run_kwargs["networks"] == ["none"]
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        assert key not in envs


async def test_execute_preserves_no_network_semantics_when_network_is_disallowed():
    # Given
    client = FakeDockerClient(wait_result=0)
    backend = make_backend(client, allow_network=False)

    # When
    result = await backend.execute("echo hi", "bash", exec_timeout=5)

    # Then
    assert result.success is True
    assert client.run_args == ("bash:5.3", ["bash", "-c", "echo hi"])
    assert client.run_kwargs is not None
    assert client.run_kwargs["networks"] == ["none"]


async def test_execute_pulls_image_when_image_is_missing():
    # Given
    client = FakeDockerClient(image_exists=False, wait_result=0)
    backend = make_backend(client)

    # When
    result = await backend.execute("console.log('hi')", "node", exec_timeout=5)

    # Then
    assert result.success is True
    assert client.image.exists_calls == ["node:26.12.1-slim"]
    assert client.image.pull_calls == ["node:26.12.1-slim"]


async def test_execute_unsupported_language_does_not_touch_docker_client(monkeypatch):
    # Given
    def fail_if_loaded():
        raise AssertionError("Docker client should not be loaded for unsupported languages")

    monkeypatch.setattr(docker_backend_module, "_get_docker", fail_if_loaded)
    backend = DockerBackend(SandboxConfig())

    # When
    result = await backend.execute("puts 'hi'", "ruby", exec_timeout=5)

    # Then
    assert result.success is False
    assert result.exit_code == 1
    assert result.error == "Unsupported language: ruby. Use: python, bash, node"


async def test_execute_timeout_returns_exit_code_124_and_removes_container():
    # Given
    client = FakeDockerClient(wait_error=TimeoutError("timeout waiting for container"))
    backend = make_backend(client)

    # When
    result = await backend.execute("sleep 60", "bash", exec_timeout=3)

    # Then
    assert result.success is False
    assert result.exit_code == 124
    assert result.error == "Code execution timed out after 3s"
    assert client.container.remove_calls == [(client.created_container, True)]


async def test_execute_cleanup_error_does_not_mask_execution_result():
    # Given
    client = FakeDockerClient(wait_result=0, remove_error=RuntimeError("remove failed"))
    backend = make_backend(client)

    # When
    result = await backend.execute("print('hello')", "python", exec_timeout=5)

    # Then
    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "stdout text"
    assert client.container.remove_calls == [(client.created_container, True)]


async def test_execute_non_timeout_error_preserves_docker_error_prefix():
    # Given
    client = FakeDockerClient(run_error=RuntimeError("daemon refused request"))
    backend = make_backend(client)

    # When
    result = await backend.execute("print('hello')", "python", exec_timeout=5)

    # Then
    assert result.success is False
    assert result.exit_code == 1
    assert result.error == "Docker execution error: daemon refused request"


async def test_health_check_uses_info_and_returns_true_for_healthy_client():
    # Given
    backend = make_backend(FakeDockerClient())

    # When
    healthy = await backend.health_check()

    # Then
    assert healthy is True


async def test_health_check_uses_info_and_returns_false_for_unhealthy_client():
    # Given
    backend = make_backend(FakeDockerClient(info_error=RuntimeError("daemon unavailable")))

    # When
    healthy = await backend.health_check()

    # Then
    assert healthy is False


class StreamingContainerApi(FakeContainerApi):
    def __init__(self, chunks: Iterable[tuple[str, bytes]], *, blocked_stage: str = "") -> None:
        super().__init__()
        self.chunks = chunks
        self.blocked_stage = blocked_stage
        self.started = threading.Event()
        self.release = threading.Event()
        self.worker_finished = threading.Event()
        self.consumed = 0
        self.log_thread: int | None = None

    @override
    def wait(self, container: FakeContainer) -> int:
        if self.blocked_stage == "wait":
            self.started.set()
            try:
                assert self.release.wait(timeout=5)
            finally:
                self.worker_finished.set()
        return super().wait(container)

    @override
    def logs(self, container: FakeContainer, stream: bool = False) -> Iterator[tuple[str, bytes]]:
        self.logs_calls.append((container, stream))
        self.log_thread = threading.get_ident()
        try:
            for chunk in self.chunks:
                self.consumed += 1
                yield chunk
            if self.blocked_stage == "logs":
                self.started.set()
                assert self.release.wait(timeout=5)
        finally:
            self.worker_finished.set()

    @override
    def remove(self, container: FakeContainer, force: bool = True) -> None:
        super().remove(container, force)
        self.release.set()


@pytest.mark.parametrize(
    ("stdout_limit", "stderr_limit", "expected_stdout", "expected_stderr"),
    [(0, 0, "", ""), (3, 1, "A\ufffd", "\ufffd"), (7, 4, "A\u20acBCD", "\u00e9\ufffd!")],
)
async def test_execute_retains_interleaved_raw_byte_prefixes(
    stdout_limit: int, stderr_limit: int, expected_stdout: str, expected_stderr: str
) -> None:
    # Given: interleaved chunks split code points, include invalid bytes, and exceed both budgets.
    api = StreamingContainerApi(
        iter((("stdout", b"A\xe2"), ("stderr", b"\xc3"), ("stdout", b"\x82\xacBCDE"), ("stderr", b"\xa9\xff!end")))
    )
    client = FakeDockerClient()
    client.container = api
    backend = make_backend(client, max_stdout_bytes=stdout_limit, max_stderr_bytes=stderr_limit)
    # When
    result = await backend.execute("pass", "python")
    # Then: the finite synchronous iterator is exhausted off-loop, even with zero retained bytes.
    assert result.success
    assert (result.stdout, result.stderr) == (expected_stdout, expected_stderr)
    assert api.consumed == 4
    assert api.log_thread != threading.get_ident()
    assert api.remove_calls == [(client.created_container, True)]


async def test_execute_large_interleaved_output_has_bounded_retention_and_display() -> None:
    # Given: a lazy SDK iterator emits eight MiB on each stream.
    def chunks() -> Iterator[tuple[str, bytes]]:
        for _ in range(2048):
            yield "stdout", b"s" * 4096
            yield "stderr", b"e" * 4096

    client = FakeDockerClient()
    api = StreamingContainerApi(chunks())
    client.container = api
    backend = make_backend(client, max_stdout_bytes=12000, max_stderr_bytes=7000)
    # When
    tracemalloc.start()
    try:
        result = await backend.execute("pass", "python")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # Then: application buffers stay small without early iterator termination or altered display clips.
    assert result.success
    assert (result.stdout, result.stderr) == ("s" * 10000, "e" * 5000)
    assert api.consumed == 4096
    assert peak < 2_000_000
    assert api.remove_calls == [(client.created_container, True)]


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("stage", ["wait", "logs"])
async def test_execute_timeout_and_cancellation_remove_owned_container_while_worker_is_blocked(
    monkeypatch: pytest.MonkeyPatch, cancel: bool, stage: str
) -> None:
    # Given: either wait blocks, or log collection blocks after overflowing both budgets.
    api = StreamingContainerApi((("stdout", b"s" * 4096), ("stderr", b"e" * 4096)), blocked_stage=stage)
    client = FakeDockerClient()
    client.container = api
    backend = make_backend(client, max_stdout_bytes=3, max_stderr_bytes=2)
    scopes: list[CancelScope] = []
    fail_after = docker_backend_module.fail_after

    @contextmanager
    def controlled_deadline(delay: float) -> Generator[CancelScope]:
        with fail_after(delay) as scope:
            scopes.append(scope)
            yield scope

    monkeypatch.setattr(docker_backend_module, "fail_after", controlled_deadline)
    # When: cancellation or the configured deadline interrupts an active synchronous SDK worker.
    execution = asyncio.create_task(backend.execute("pass", "python", exec_timeout=30))
    try:
        assert await asyncio.to_thread(api.started.wait, 5)
        if cancel:
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        else:
            scopes[0].deadline = current_time() - 1
            result = await execution
            assert not result.success
            assert result.exit_code == 124
            assert result.error == "Code execution timed out after 30s"
        # Then: removal releases the worker; overflow cannot suppress timeout/cancellation cleanup.
        assert api.remove_calls == [(client.created_container, True)]
        assert await asyncio.to_thread(api.worker_finished.wait, 5)
        assert api.consumed == (2 if stage == "logs" else 0)
    finally:
        api.release.set()
        execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
