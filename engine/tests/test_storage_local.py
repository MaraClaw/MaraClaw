from __future__ import annotations

import multiprocessing
import os
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Barrier
from pathlib import Path
from threading import Event

import anyio
import pytest

from app.services.storage_runtime.base import (
    AtomicCreateUnsupportedError,
    ConditionalWriteResult,
    StorageBackend,
    WriteCondition,
)
from app.services.storage_runtime.local import LocalStorageBackend


async def test_local_absence_condition_has_one_winner_when_writers_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    storage = LocalStorageBackend(str(tmp_path))
    original_version = storage.get_version
    observed = 0
    both_observed = anyio.Event()
    results: list[ConditionalWriteResult] = []
    payloads = (b"first complete upload", b"second complete upload")

    async def synchronized_version(key: str):
        nonlocal observed
        version = await original_version(key)
        observed += 1
        if observed <= 2:
            if observed == 2:
                both_observed.set()
            await both_observed.wait()
        return version

    monkeypatch.setattr(storage, "get_version", synchronized_version)

    async def create(data: bytes) -> None:
        results.append(
            await storage.write_bytes_if_match("report.txt", data, condition=WriteCondition(require_absent=True))
        )

    async with anyio.create_task_group() as tasks:
        for payload in payloads:
            tasks.start_soon(create, payload)

    assert sum(result.ok for result in results) == 1
    assert sum(result.conflict for result in results) == 1
    assert (tmp_path / "report.txt").read_bytes() in payloads


def create_from_process(root: str, payload: bytes, barrier: Barrier, result_pipe: Connection) -> None:
    storage = LocalStorageBackend(root)
    barrier.wait(timeout=20)
    result = anyio.run(storage.create_bytes_if_absent, "report.txt", payload)
    result_pipe.send(result.ok)
    result_pipe.close()


def test_local_atomic_creation_has_one_complete_winner_across_processes(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    payloads = [bytes([index]) * 100_000 for index in range(4)]
    channels = [context.Pipe(duplex=False) for _payload in payloads]
    processes = [
        context.Process(target=create_from_process, args=(str(tmp_path), payload, barrier, writer))
        for payload, (_reader, writer) in zip(payloads, channels, strict=True)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)
        assert all(process.exitcode == 0 for process in processes)
        assert all(reader.poll(1) for reader, _writer in channels)
        assert sum(reader.recv() for reader, _writer in channels) == 1
        assert (tmp_path / "report.txt").read_bytes() in payloads
        assert sorted(path.name for path in tmp_path.iterdir()) == ["report.txt"]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        for reader, writer in channels:
            reader.close()
            writer.close()


async def test_local_key_is_invisible_until_complete_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    storage = LocalStorageBackend(str(tmp_path))
    publication_ready = Event()
    publish = Event()
    original_link = os.link
    payload = b"complete upload" * 10_000

    def pause_publication(source: Path, destination: Path) -> None:
        assert source.read_bytes() == payload
        publication_ready.set()
        assert publish.wait(timeout=10)
        original_link(source, destination)

    async def create() -> None:
        assert (await storage.create_bytes_if_absent("report.txt", payload)).ok

    monkeypatch.setattr(os, "link", pause_publication)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(create)
        try:
            assert await anyio.to_thread.run_sync(publication_ready.wait, 10)
            assert not await storage.exists("report.txt")
        finally:
            publish.set()
    assert (tmp_path / "report.txt").read_bytes() == payload
    assert sorted(await anyio.to_thread.run_sync(os.listdir, tmp_path)) == ["report.txt"]


@pytest.mark.parametrize("failure_stage", ["fsync", "link"])
async def test_local_atomic_create_cleans_temporary_files_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
):
    storage = LocalStorageBackend(str(tmp_path))

    def fail(*args) -> None:
        raise OSError("injected storage failure")

    monkeypatch.setattr(os, failure_stage, fail)

    with pytest.raises(OSError, match="injected storage failure"):
        await storage.create_bytes_if_absent("report.txt", b"complete")

    assert await anyio.to_thread.run_sync(os.listdir, tmp_path) == []


async def test_atomic_creation_is_unsupported_instead_of_emulated_by_base_backend():
    with pytest.raises(AtomicCreateUnsupportedError):
        await StorageBackend().create_bytes_if_absent("report.txt", b"complete")


async def test_local_atomic_creation_preserves_existing_directory(tmp_path: Path):
    storage = LocalStorageBackend(str(tmp_path))
    (tmp_path / "report.txt").mkdir()

    result = await storage.create_bytes_if_absent("report.txt", b"complete")

    assert result.conflict
    assert not result.ok
    assert (tmp_path / "report.txt").is_dir()
    assert sorted(await anyio.to_thread.run_sync(os.listdir, tmp_path)) == ["report.txt"]
