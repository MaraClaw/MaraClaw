import asyncio
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
async def test_proxy_retries_reaps_and_removes_secret_files(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setenv("DISCORD_PROXY", "existing-proxy")
    # When
    await owner.start()
    assert processes[0].reaped
    assert not paths[0].exists()
    assert paths[1].exists()
    await owner.stop()
    # Then
    assert processes[1].terminated
    assert processes[1].reaped
    assert not any(path.exists() for path in paths)
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
