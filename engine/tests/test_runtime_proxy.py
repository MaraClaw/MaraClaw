import asyncio
import json
from pathlib import Path

import pytest

from app.runtime import proxy


class FakeProcess:
    def __init__(self, exited: bool = False) -> None:
        self.returncode: int | None = 1 if exited else None
        self.stderr: asyncio.StreamReader = asyncio.StreamReader()
        self.stderr.feed_data(b"failed node")
        self.stderr.feed_eof()
        self.reaped: bool = False
        self.terminated: bool = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        self.reaped = True
        return self.returncode or 0


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_proxy", [None, "existing-proxy"])
async def test_proxy_retries_reaps_and_removes_secret_files(
    monkeypatch: pytest.MonkeyPatch, previous_proxy: str | None
) -> None:
    # Given
    owner = proxy.ShadowsocksProxy()
    processes = [FakeProcess(True), FakeProcess()]
    paths: list[Path] = []
    node = {"server": "host", "port": 443, "password": "secret", "method": "aes-256-gcm"}

    async def nodes():
        return [node, node]

    async def spawn(*args: str, **_kwargs: object):
        paths.append(Path(args[2]))
        return processes[len(paths) - 1]

    async def ready(_seconds: float) -> None:
        return None

    monkeypatch.setattr(owner, "_nodes", nodes)
    monkeypatch.setattr(proxy.shutil, "which", lambda _: "ss-local")
    monkeypatch.setattr(proxy.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(proxy.asyncio, "sleep", ready)
    if previous_proxy is None:
        monkeypatch.delenv("DISCORD_PROXY", raising=False)
    else:
        monkeypatch.setenv("DISCORD_PROXY", previous_proxy)
    # When
    await owner.start()
    assert processes[0].reaped
    assert not paths[0].exists()
    assert paths[1].exists()
    assert proxy.os.environ["DISCORD_PROXY"] == "socks5h://127.0.0.1:1080"
    await owner.stop()
    # Then
    assert processes[1].terminated
    assert processes[1].reaped
    assert not any(path.exists() for path in paths)
    assert proxy.os.environ.get("DISCORD_PROXY") == previous_proxy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configuration", "expected_warning"),
    [
        pytest.param(None, None, id="unconfigured"),
        pytest.param("[]", None, id="empty-list"),
        pytest.param('[null, 1, "ignored"]', None, id="non-object-list"),
        pytest.param(" \n", "Empty node config", id="empty-file"),
        pytest.param("{", "Failed to parse node config", id="malformed-json"),
        pytest.param("{}", "Config is not a node list", id="non-list"),
    ],
)
async def test_proxy_skips_without_node_failure_when_no_nodes_are_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, configuration: str | None, expected_warning: str | None
) -> None:
    # Given
    owner = proxy.ShadowsocksProxy()
    config_path = tmp_path / "nodes.json"
    if configuration is not None:
        config_path.write_text(configuration)
    warnings: list[str] = []

    def record(message: str, *args: object) -> None:
        warnings.append(message.format(*args))

    async def spawn(*_args: str, **_kwargs: object) -> None:
        pytest.fail("No child should start without a loaded node")

    monkeypatch.setenv("SS_CONFIG_FILE", str(config_path))
    monkeypatch.delenv("SS_SERVER", raising=False)
    monkeypatch.delenv("SS_PASSWORD", raising=False)
    monkeypatch.setenv("DISCORD_PROXY", "existing-proxy")
    monkeypatch.setattr(proxy.shutil, "which", lambda _: "ss-local")
    monkeypatch.setattr(proxy.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(proxy.logger, "warning", record)
    # When
    await owner.start()
    # Then
    if expected_warning is None:
        assert warnings == []
    else:
        assert len(warnings) == 1
        assert expected_warning in warnings[0]
    assert owner.process is None
    assert owner.config_path is None
    assert proxy.os.environ["DISCORD_PROXY"] == "existing-proxy"


@pytest.mark.asyncio
async def test_proxy_reports_all_failed_nodes_and_cleans_attempts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given
    owner = proxy.ShadowsocksProxy()
    processes = [FakeProcess(True), FakeProcess(True)]
    paths: list[Path] = []
    warnings: list[str] = []
    node = {"server": "host", "port": 443, "password": "secret", "method": "aes-256-gcm"}
    config_path = tmp_path / "nodes.json"
    config_path.write_text(json.dumps([{**node, "label": "first"}, {**node, "label": "second"}]))

    def record(message: str, *args: object) -> None:
        warnings.append(message.format(*args))

    async def spawn(*args: str, **_kwargs: object) -> FakeProcess:
        paths.append(Path(args[2]))
        return processes[len(paths) - 1]

    async def ready(_seconds: float) -> None:
        return None

    monkeypatch.setenv("SS_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("DISCORD_PROXY", "existing-proxy")
    monkeypatch.setattr(proxy.shutil, "which", lambda _: "ss-local")
    monkeypatch.setattr(proxy.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(proxy.asyncio, "sleep", ready)
    monkeypatch.setattr(proxy.logger, "warning", record)
    # When
    await owner.start()
    # Then
    assert warnings == [
        "[Proxy] Node first failed: failed node",
        "[Proxy] Node second failed: failed node",
        "[Proxy] All SS nodes failed - Discord API calls will run without proxy",
    ]
    assert len(paths) == 2
    assert all(process.reaped for process in processes)
    assert not any(path.exists() for path in paths)
    assert owner.process is None
    assert owner.config_path is None
    assert proxy.os.environ["DISCORD_PROXY"] == "existing-proxy"


@pytest.mark.asyncio
async def test_proxy_interrupted_acquisition_retains_and_cleans_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    owner = proxy.ShadowsocksProxy()
    process = FakeProcess()
    spawning = asyncio.Event()
    release = asyncio.Event()
    paths: list[Path] = []

    async def nodes():
        return [{"server": "host", "port": 443, "password": "secret", "method": "aes-256-gcm"}]

    async def spawn(*args: str, **_kwargs: object):
        paths.append(Path(args[2]))
        spawning.set()
        await release.wait()
        return process

    monkeypatch.setattr(owner, "_nodes", nodes)
    monkeypatch.setattr(proxy.shutil, "which", lambda _: "ss-local")
    monkeypatch.setattr(proxy.asyncio, "create_subprocess_exec", spawn)
    # When
    task = asyncio.create_task(owner.start())
    await spawning.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Then
    assert process.terminated
    assert process.reaped
    assert not paths[0].exists()
