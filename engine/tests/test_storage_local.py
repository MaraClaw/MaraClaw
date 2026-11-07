from __future__ import annotations

import multiprocessing
import os
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Barrier
from pathlib import Path
from threading import Event, get_ident

import anyio
import pytest
from fastapi import HTTPException

from app.services.storage_runtime import local as local_module
from app.services.storage_runtime.base import (
    AtomicCreateUnsupportedError,
    ConditionalWriteResult,
    StorageBackend,
    StorageReadLimitExceededError,
    WriteCondition,
    walk_order,
)
from app.services.storage_runtime.bounded import BoundedReader
from app.services.storage_runtime.local import LocalStorageBackend
from storage_walk_fixtures import (
    EXPECTED_WALK,
    FILE_SIZE,
    ListOnlyBackend,
    build_tree_on_disk,
    s3_backend,
    shape,
    tree_client,
)


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


@pytest.mark.parametrize(("limit", "size"), [(0, 0), (0, 1), (8, 0), (8, 7), (8, 8), (8, 9), (8, 200_000)])
async def test_local_bounded_read_counts_actual_bytes_and_closes(tmp_path, monkeypatch, limit, size):
    from app.services.storage_runtime import local

    target = tmp_path / "report.bin"
    target.write_bytes(b"x" * size)
    backend = LocalStorageBackend(str(tmp_path))
    original = local.read_bounded
    consumed = 0
    streams = []

    class CountedReader:
        def __init__(self, stream: BoundedReader):
            self.stream = stream

        def read(self, requested: int) -> bytes:
            nonlocal consumed
            assert 0 < requested <= limit + 1 - consumed
            chunk = self.stream.read(requested)
            consumed += len(chunk)
            return chunk

    def observe(stream, max_bytes):
        streams.append(stream)
        return original(CountedReader(stream), max_bytes)

    async def forbidden(*_args):
        pytest.fail("bounded read must not hash or fully read the file")

    monkeypatch.setattr(local, "read_bounded", observe)
    for operation in ("read_bytes", "get_version", "stat"):
        monkeypatch.setattr(backend, operation, forbidden)

    if size > limit:
        with pytest.raises(StorageReadLimitExceededError) as raised:
            await backend.read_bytes_bounded("report.bin", limit)
        assert raised.value.size_bytes == limit + 1
    else:
        assert await backend.read_bytes_bounded("report.bin", limit) == b"x" * size

    assert consumed == min(size, limit + 1)
    assert len(streams) == 1
    assert streams[0].closed


@pytest.mark.parametrize("growth", ["after_inspection", "during_read"])
async def test_local_bounded_read_rejects_real_file_growth(tmp_path, monkeypatch, growth):
    from app.services.storage_runtime import local

    target = tmp_path / "growing.bin"
    target.write_bytes(b"ok")
    backend = LocalStorageBackend(str(tmp_path))
    assert await backend.is_file("growing.bin")
    original = local.read_bounded
    consumed = 0
    streams = []

    def grow():
        with target.open("ab", buffering=0) as writer:
            writer.write(b"x" * 200_000)

    class GrowingReader:
        def __init__(self, stream: BoundedReader):
            self.stream = stream

        def read(self, requested: int) -> bytes:
            nonlocal consumed
            chunk = self.stream.read(min(requested, 2) if consumed == 0 else requested)
            if consumed == 0 and growth == "during_read":
                grow()
            consumed += len(chunk)
            return chunk

    def observe(stream, max_bytes):
        streams.append(stream)
        return original(GrowingReader(stream), max_bytes)

    monkeypatch.setattr(local, "read_bounded", observe)
    if growth == "after_inspection":
        grow()

    with pytest.raises(StorageReadLimitExceededError):
        await backend.read_bytes_bounded("growing.bin", 65_536)

    assert consumed == 65_537
    assert target.stat().st_size == 200_002
    assert streams[0].closed


async def test_local_bounded_read_closes_file_on_read_error(tmp_path, monkeypatch):
    from app.services.storage_runtime import local

    (tmp_path / "report.bin").write_bytes(b"document")
    streams = []

    def fail(stream, max_bytes):
        streams.append(stream)
        raise OSError("read failed")

    monkeypatch.setattr(local, "read_bounded", fail)

    with pytest.raises(OSError, match="read failed"):
        await LocalStorageBackend(str(tmp_path)).read_bytes_bounded("report.bin", 8)

    assert streams[0].closed


async def test_local_bounded_read_preserves_missing_file_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        await LocalStorageBackend(str(tmp_path)).read_bytes_bounded("missing.bin", 8)


async def test_base_bounded_read_cannot_fall_back_to_full_read(monkeypatch):
    backend = StorageBackend()

    async def forbidden(*_args):
        pytest.fail("bounded contract must not emulate a bound with a full read")

    monkeypatch.setattr(backend, "read_bytes", forbidden)

    with pytest.raises(NotImplementedError):
        await backend.read_bytes_bounded("report.bin", 8)


@pytest.mark.parametrize("occupied", [False, True])
async def test_local_atomic_create_metadata_never_rereads_content(tmp_path, monkeypatch, occupied):
    backend = LocalStorageBackend(str(tmp_path))
    target = tmp_path / "report.bin"
    if occupied:
        with target.open("wb") as stream:
            stream.truncate(100_000_000)

    async def forbidden(*_args):
        pytest.fail("atomic create metadata must not reread the created file or conflict winner")

    for operation in ("read_bytes", "get_version", "stat"):
        monkeypatch.setattr(backend, operation, forbidden)

    result = await backend.create_bytes_if_absent("report.bin", b"new")

    assert result.ok is not occupied
    assert result.conflict is occupied
    if occupied:
        assert target.stat().st_size == 100_000_000
    else:
        assert result.current_version is not None
        assert result.current_version.size == 3
        assert target.read_bytes() == b"new"


@pytest.mark.parametrize("key", ["link/secret.txt", "link"])
async def test_local_storage_rejects_symlink_to_prefix_sibling_of_root(tmp_path: Path, key: str):
    root = tmp_path / "storage"
    sibling = tmp_path / "storage-other"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret")
    (root / "link").symlink_to(sibling, target_is_directory=True)
    backend = LocalStorageBackend(str(root))

    with pytest.raises(HTTPException) as raised:
        backend._full_path(key)
    with pytest.raises(HTTPException):
        await backend.read_bytes(key)
    with pytest.raises(HTTPException):
        await backend.write_bytes("link/written.txt", b"escaped")

    assert raised.value.status_code == 403
    assert raised.value.detail == "Path traversal not allowed"
    assert sorted(path.name for path in sibling.iterdir()) == ["secret.txt"]


async def test_local_storage_keeps_symlinks_that_stay_inside_root(tmp_path: Path):
    root = tmp_path / "storage"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.txt").write_text("inside")
    (root / "alias").symlink_to(root / "real", target_is_directory=True)
    backend = LocalStorageBackend(str(root))

    assert backend._full_path("alias/a.txt") == (root / "real" / "a.txt").resolve()
    assert await backend.read_bytes("alias/a.txt") == b"inside"


async def test_local_s3_and_list_only_backends_walk_the_same_tree_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given
    build_tree_on_disk(tmp_path)
    local = LocalStorageBackend(str(tmp_path))
    backends: tuple[StorageBackend, ...] = (local, s3_backend(tree_client(), monkeypatch), ListOnlyBackend(local))
    # When
    walks = [[entry async for entry in backend.walk("w")] for backend in backends]
    # Then
    for entries in walks:
        assert shape(entries) == list(EXPECTED_WALK)
        assert {entry.size for entry in entries if not entry.is_dir} == {FILE_SIZE}


async def test_local_probes_listing_and_walk_run_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Given
    build_tree_on_disk(tmp_path)
    backend = LocalStorageBackend(str(tmp_path))
    resolve_threads: list[int] = []
    resolve = backend._full_path

    def spy(key: str) -> Path:
        resolve_threads.append(get_ident())
        return resolve(key)

    monkeypatch.setattr(backend, "_full_path", spy)
    # When
    assert await backend.exists("w")
    assert await backend.is_dir("w")
    assert await backend.is_file("w/a.txt")
    _ = await backend.list_dir("w")
    _ = [entry async for entry in backend.walk("w")]
    # Then
    assert len(resolve_threads) > 4
    assert get_ident() not in resolve_threads


async def test_local_walk_scans_one_directory_at_a_time_and_stops_when_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given
    build_tree_on_disk(tmp_path)
    backend = LocalStorageBackend(str(tmp_path))
    scanned: list[str] = []
    scan = backend._scan_directory

    def spy(key: str):
        scanned.append(key)
        return scan(key)

    monkeypatch.setattr(backend, "_scan_directory", spy)
    # When
    walk = backend.walk("w")
    names = [(await anext(walk)).name for _ in range(4)]
    await walk.aclose()
    # Then
    assert names == ["B.txt", "a-b.txt", "a.txt", "a"]
    assert scanned == ["w"]


@pytest.mark.parametrize("operation", ["exists", "is_dir", "is_file", "list_dir", "walk"])
async def test_local_probes_listing_and_walk_answer_403_for_keys_that_escape_the_root(tmp_path: Path, operation: str):
    # Given
    root = tmp_path / "storage"
    sibling = tmp_path / "storage-other"
    root.mkdir()
    sibling.mkdir()
    (root / "link").symlink_to(sibling, target_is_directory=True)
    backend = LocalStorageBackend(str(root))

    # When
    async def attempt() -> None:
        if operation == "walk":
            _ = [entry async for entry in backend.walk("link")]
        else:
            _ = await getattr(backend, operation)("link")

    with pytest.raises(HTTPException) as raised:
        await attempt()
    # Then
    assert raised.value.status_code == 403


# Sibling order of "w" by walk_order: hidden names stay (the API filters them), symlinks and .gitkeep never appear.
PAGED_ROOT = (
    ".env",
    ".hidden/",
    "B.txt",
    "a-b.txt",
    "a.txt",
    "a/",
    "a0",
    "b/",
    "empty/",
    "\u00e9.txt",
)


async def drain_pages(backend: StorageBackend, key: str, limit: int) -> list[list[str]]:
    pages: list[list[str]] = []
    after = ""
    while True:
        page = await backend.list_dir_page(key, limit=limit, after=after)
        pages.append([walk_order(entry) for entry in page.entries])
        if not page.has_more:
            return pages
        after = walk_order(page.entries[-1])


async def test_local_s3_and_list_only_backends_page_the_same_tree_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given
    build_tree_on_disk(tmp_path)
    local = LocalStorageBackend(str(tmp_path))
    backends: tuple[StorageBackend, ...] = (local, s3_backend(tree_client(), monkeypatch), ListOnlyBackend(local))
    # When
    paged = [await drain_pages(backend, "w", 3) for backend in backends]
    # Then
    for pages in paged:
        assert pages == [list(PAGED_ROOT[index : index + 3]) for index in range(0, len(PAGED_ROOT), 3)]


async def test_local_page_resumes_after_a_folder_and_after_a_file(tmp_path: Path):
    # Given
    build_tree_on_disk(tmp_path)
    backend = LocalStorageBackend(str(tmp_path))
    # When
    after_folder = await backend.list_dir_page("w", limit=2, after="a/")
    after_file = await backend.list_dir_page("w", limit=2, after="a.txt")
    last = await backend.list_dir_page("w", limit=5, after="empty/")
    # Then
    assert [walk_order(entry) for entry in after_folder.entries] == ["a0", "b/"]
    assert after_folder.has_more
    assert [walk_order(entry) for entry in after_file.entries] == ["a/", "a0"]
    assert [walk_order(entry) for entry in last.entries] == ["\u00e9.txt"]
    assert not last.has_more


async def test_local_page_stats_only_the_returned_slice_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given
    for index in range(40):
        _ = (tmp_path / f"f{index:02d}").write_bytes(b"x")
    backend = LocalStorageBackend(str(tmp_path))
    built: list[str] = []
    threads: list[int] = []
    entry_for = local_module._storage_entry
    resolve = backend._full_path

    def spy_entry(entry: Path, root: Path):
        built.append(entry.name)
        return entry_for(entry, root)

    def spy_path(key: str) -> Path:
        threads.append(get_ident())
        return resolve(key)

    monkeypatch.setattr(local_module, "_storage_entry", spy_entry)
    monkeypatch.setattr(backend, "_full_path", spy_path)
    # When
    page = await backend.list_dir_page("", limit=5, after="f09")
    # Then
    assert built == ["f10", "f11", "f12", "f13", "f14"]
    assert (len(page.entries), page.has_more) == (5, True)
    assert threads
    assert get_ident() not in threads


async def test_local_page_of_a_missing_directory_is_empty(tmp_path: Path):
    page = await LocalStorageBackend(str(tmp_path)).list_dir_page("nowhere", limit=3)

    assert (page.entries, page.has_more) == ([], False)


async def test_local_page_answers_403_for_a_key_that_escapes_the_root(tmp_path: Path):
    # Given
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    # When
    with pytest.raises(HTTPException) as raised:
        _ = await LocalStorageBackend(str(root)).list_dir_page("link", limit=3)
    # Then
    assert raised.value.status_code == 403
