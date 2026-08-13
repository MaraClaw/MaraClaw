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
    def __init__(self, wait_result=0, wait_error: Exception | None = None, remove_error: Exception | None = None):
        self.wait_result = wait_result
        self.wait_error = wait_error
        self.remove_error = remove_error
        self.wait_calls = []
        self.logs_calls = []
        self.remove_calls = []

    def wait(self, container: FakeContainer):
        self.wait_calls.append(container)
        if self.wait_error is not None:
            raise self.wait_error
        return self.wait_result

    def logs(self, container: FakeContainer, stream: bool = False):
        self.logs_calls.append((container, stream))
        return [("stdout", b"stdout text"), ("stderr", b"stderr text")]

    def remove(self, container: FakeContainer, force: bool = True) -> None:
        self.remove_calls.append((container, force))
        if self.remove_error is not None:
            raise self.remove_error


class FakeDockerClient:
    def __init__(
        self,
        image_exists: bool = True,
        wait_result=0,
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
        self.run_kwargs = None
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
    backend._client = client
    return backend


async def test_execute_runs_python_container_with_python_on_whales_kwargs():
    # Given
    client = FakeDockerClient(wait_result=0)
    backend = make_backend(client, allow_network=True)

    # When
    result = await backend.execute("print('hello')", "python", timeout=7)

    # Then
    assert result.success is True
    assert result.stdout == "stdout text"
    assert result.stderr == "stderr text"
    assert result.exit_code == 0
    assert result.error is None
    assert client.image.exists_calls == ["python:3.14.6-slim"]
    assert client.image.pull_calls == []
    assert client.run_args == ("python:3.14.6-slim", ["python3", "-c", "print('hello')"])
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
    result = await backend.execute("print('hello')", "python", timeout=5)

    # Then
    assert result.success is True
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
    result = await backend.execute("echo hi", "bash", timeout=5)

    # Then
    assert result.success is True
    envs = client.run_kwargs["envs"]
    assert client.run_kwargs["networks"] == ["none"]
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        assert key not in envs


async def test_execute_preserves_no_network_semantics_when_network_is_disallowed():
    # Given
    client = FakeDockerClient(wait_result=0)
    backend = make_backend(client, allow_network=False)

    # When
    result = await backend.execute("echo hi", "bash", timeout=5)

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
    result = await backend.execute("console.log('hi')", "node", timeout=5)

    # Then
    assert result.success is True
    assert client.image.exists_calls == ["node:26.5.0-slim"]
    assert client.image.pull_calls == ["node:26.5.0-slim"]


async def test_execute_unsupported_language_does_not_touch_docker_client(monkeypatch):
    # Given
    def fail_if_loaded():
        raise AssertionError("Docker client should not be loaded for unsupported languages")

    monkeypatch.setattr(docker_backend_module, "_get_docker", fail_if_loaded)
    backend = DockerBackend(SandboxConfig())

    # When
    result = await backend.execute("puts 'hi'", "ruby", timeout=5)

    # Then
    assert result.success is False
    assert result.exit_code == 1
    assert result.error == "Unsupported language: ruby. Use: python, bash, node"


async def test_execute_timeout_returns_exit_code_124_and_removes_container():
    # Given
    client = FakeDockerClient(wait_error=TimeoutError("timeout waiting for container"))
    backend = make_backend(client)

    # When
    result = await backend.execute("sleep 60", "bash", timeout=3)

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
    result = await backend.execute("print('hello')", "python", timeout=5)

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
    result = await backend.execute("print('hello')", "python", timeout=5)

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
