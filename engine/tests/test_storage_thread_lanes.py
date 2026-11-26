import asyncio
from collections.abc import Iterator
from pathlib import Path
from threading import Event, Lock, Semaphore

import pytest

from app.core.thread_lanes import ThreadLane
from app.services.storage_runtime.s3 import S3StorageBackend
from s3_wire_server import S3Server
from thread_lane_fixtures import forbid_shared_threads


@pytest.fixture
def server() -> Iterator[S3Server]:
    wire = S3Server()
    wire.start()
    try:
        yield wire
    finally:
        wire.stop()


def backend_for(server: S3Server) -> S3StorageBackend:
    return S3StorageBackend(
        bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
    )


class Probe:
    """Blocking work that records how many calls run at once."""

    def __init__(self) -> None:
        self.entered = Semaphore(0)
        self.gate = Event()
        self.running = 0
        self.peak = 0
        self.started = 0
        self._lock = Lock()

    def work(self) -> str:
        with self._lock:
            self.started += 1
            self.running += 1
            self.peak = max(self.peak, self.running)
        self.entered.release()
        assert self.gate.wait(10)
        with self._lock:
            self.running -= 1
        return "done"

    async def wait_entered(self, count: int) -> None:
        for _ in range(count):
            assert await asyncio.to_thread(self.entered.acquire, True, 10)


@pytest.mark.parametrize(("lane_size", "pool", "expected"), [(32, 3, 3), (2, 50, 2), (5, 5, 5)])
def test_lane_never_outgrows_the_boto_pool(lane_size, pool, expected):
    backend = S3StorageBackend(bucket="bucket", max_pool_connections=pool, thread_lane_size=lane_size)
    assert backend.lane.size == expected
    assert backend.max_pool_connections >= backend.lane.size


async def test_lane_runs_at_most_its_size_and_queues_the_rest():
    lane, probe = ThreadLane(2), Probe()
    calls = [asyncio.create_task(lane.run(probe.work)) for _ in range(5)]
    await probe.wait_entered(2)
    await asyncio.sleep(0)
    assert probe.started == 2
    probe.gate.set()
    assert await asyncio.wait_for(asyncio.gather(*calls), 10) == ["done"] * 5
    assert probe.peak == 2


async def test_abandoned_call_returns_at_once_but_keeps_its_slot_until_the_thread_ends():
    lane, held, follower = ThreadLane(1), Probe(), Probe()
    follower.gate.set()
    abandoned = asyncio.create_task(lane.run(held.work, abandon_on_cancel=True))
    await held.wait_entered(1)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    queued = asyncio.create_task(lane.run(follower.work))
    await asyncio.sleep(0)
    assert follower.started == 0
    held.gate.set()
    assert await asyncio.wait_for(queued, 10) == "done"
    assert (held.peak, follower.peak) == (1, 1)


async def test_a_call_cancelled_while_waiting_for_a_slot_never_runs_and_holds_nothing():
    lane, held, skipped, later = ThreadLane(1), Probe(), Probe(), Probe()
    later.gate.set()
    running = asyncio.create_task(lane.run(held.work))
    await held.wait_entered(1)
    waiting = asyncio.create_task(lane.run(skipped.work))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    held.gate.set()
    assert await asyncio.wait_for(running, 10) == "done"
    assert await asyncio.wait_for(lane.run(later.work), 10) == "done"
    assert skipped.started == 0


async def test_work_errors_reach_the_caller_and_free_the_slot():
    lane = ThreadLane(1)

    def fail() -> None:
        raise OSError("provider down")

    with pytest.raises(OSError, match="provider down"):
        await lane.run(fail)
    assert await asyncio.wait_for(lane.run(lambda: 7), 10) == 7


async def test_every_s3_operation_runs_on_the_lane_and_none_on_shared_threads(
    server: S3Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    backend = backend_for(server)
    source = tmp_path / "source.bin"
    source.write_bytes(b"from a file")
    with forbid_shared_threads(monkeypatch):
        await backend.write_bytes("dir/a.txt", b"alpha", "text/plain")
        assert (await backend.create_bytes_if_absent("dir/b.txt", b"beta")).ok
        assert (await backend.create_file_if_absent("dir/c.bin", source)).ok
        assert await backend.exists("dir/a.txt")
        assert await backend.is_file("dir/b.txt")
        assert await backend.is_dir("dir")
        assert (await backend.get_version("dir/a.txt")).exists
        assert (await backend.stat("dir/b.txt")).size == 4
        assert await backend.read_bytes("dir/a.txt") == b"alpha"
        assert await backend.read_bytes_bounded("dir/a.txt", 5) == b"alpha"
        assert (await backend.read_snapshot_bounded("dir/b.txt", 10)).data == b"beta"
        assert [entry.name for entry in await backend.list_dir("dir")] == ["a.txt", "b.txt", "c.bin"]
        assert len((await backend.list_dir_page("dir", limit=2)).entries) == 2
        assert [entry.name async for entry in backend.walk("dir")] == ["a.txt", "b.txt", "c.bin"]
        assert [entry.name async for entry in backend.list_files_flat("dir")] == ["a.txt", "b.txt", "c.bin"]
        assert await backend.presign_download_url("dir/a.txt", "a.txt") is not None
        materialized = await backend.materialize("dir/c.bin")
        try:
            assert materialized.path.read_bytes() == b"from a file"
        finally:
            materialized.close()
        await backend.delete("dir/a.txt")
        await backend.delete_tree("dir")
    assert server.state.objects == {}
