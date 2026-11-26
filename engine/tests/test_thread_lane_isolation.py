"""Saturating the storage lane, the parser lane or neither must leave the other two (and the default executor) free."""

import asyncio
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event, Semaphore

import pytest

from app.services.document_parser import runtime
from app.services.storage_runtime.s3 import S3StorageBackend
from s3_wire_server import Gate, S3Server
from thread_lane_fixtures import forbid_shared_threads


@pytest.fixture
def server() -> Iterator[S3Server]:
    wire = S3Server()
    wire.start()
    try:
        yield wire
    finally:
        wire.stop()


class FakeProcess:
    """A parser child that finishes at once, or stays alive until `release` is set."""

    pid: int | None = None
    exitcode: int | None = 0

    def __init__(
        self, target: Callable[..., object], args: tuple[object, ...], release: Event | None, started: Semaphore
    ):
        self.target, self.args, self.release, self.started = target, args, release, started

    def start(self) -> None:
        self.target(*self.args)
        self.started.release()

    def join(self, timeout: float | None = None) -> None:
        if self.release is not None:
            _ = self.release.wait(timeout)

    def is_alive(self) -> bool:
        return self.release is not None and not self.release.is_set()

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def use_fake_parser(monkeypatch: pytest.MonkeyPatch, release: Event | None = None) -> Semaphore:
    started = Semaphore(0)

    def echo_worker(_src: str, _filename: str, out: str, _err: str, _extra: int | None) -> None:
        _ = Path(out).write_text("parsed", encoding="utf-8")

    class Context:
        def Process(self, **kwargs: object) -> FakeProcess:  # noqa: N802
            target, args = kwargs["target"], kwargs["args"]
            assert callable(target)
            assert isinstance(args, tuple)
            return FakeProcess(target, args, release, started)

    monkeypatch.setattr(runtime, "convert_worker", echo_worker)
    monkeypatch.setattr(runtime.mp, "get_context", lambda _: Context())
    return started


def convert() -> asyncio.Task[str]:
    return asyncio.create_task(
        runtime.convert_document_isolated_async(b"document", "report.docx", slot_wait_seconds=30, timeout_seconds=30)
    )


async def test_saturated_storage_lane_leaves_parser_and_default_executor_free(
    server: S3Server, monkeypatch: pytest.MonkeyPatch
):
    # Given: both lane threads are inside the provider, a third write waits for a slot
    backend = S3StorageBackend(
        bucket="bucket",
        endpoint_url=server.endpoint,
        access_key_id="key",
        secret_access_key="secret",
        thread_lane_size=2,
    )
    use_fake_parser(monkeypatch)
    gates = {key: server.state.gates.setdefault(key, Gate()) for key in ("a", "b")}
    writes = [asyncio.create_task(backend.write_bytes(key, b"data")) for key in ("a", "b", "c")]
    for gate in gates.values():
        assert await asyncio.to_thread(gate.arrived.wait, 10)
    try:
        # When / Then
        assert await asyncio.wait_for(convert(), 10) == "parsed"
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 10) == "free"
        assert server.state.count("PUT", "c") == 0
    finally:
        for gate in gates.values():
            gate.release.set()
    await asyncio.wait_for(asyncio.gather(*writes), 10)
    assert set(server.state.objects) == {"a", "b", "c"}


async def test_saturated_parser_leaves_storage_lane_and_default_executor_free(
    server: S3Server, monkeypatch: pytest.MonkeyPatch
):
    # Given: every parser slot runs a child that has not finished, and a third job waits for a slot
    backend = S3StorageBackend(
        bucket="bucket", endpoint_url=server.endpoint, access_key_id="key", secret_access_key="secret"
    )
    server.state.objects["doc"] = b"stored"
    release = Event()
    started = use_fake_parser(monkeypatch, release)
    running = [convert() for _ in range(runtime.MAX_CONCURRENCY)]
    try:
        for _ in running:
            assert await asyncio.to_thread(started.acquire, True, 10)
        threads_before = threading.active_count()
        waiting = convert()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert threading.active_count() == threads_before, "a waiting parser job must not park a thread"
        # When / Then
        await asyncio.wait_for(backend.write_bytes("new", b"fresh"), 10)
        assert await asyncio.wait_for(backend.read_bytes("doc"), 10) == b"stored"
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 10) == "free"
        assert not waiting.done()
    finally:
        release.set()
    assert await asyncio.wait_for(asyncio.gather(*running, waiting), 10) == ["parsed"] * (runtime.MAX_CONCURRENCY + 1)


async def test_parser_never_reaches_the_default_executor(monkeypatch: pytest.MonkeyPatch):
    use_fake_parser(monkeypatch)
    with forbid_shared_threads(monkeypatch):
        assert await asyncio.wait_for(convert(), 10) == "parsed"
