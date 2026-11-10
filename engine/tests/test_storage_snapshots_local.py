import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import anyio
import pytest
from fastapi import HTTPException

from app.services.storage_runtime import local_snapshot
from app.services.storage_runtime.base import StorageBackend, StorageReadChangedError, StorageReadLimitExceededError
from app.services.storage_runtime.bounded import BoundedReader
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.mark.parametrize("size", [0, 7, 8, 9])
async def test_local_snapshot_reads_and_hashes_once_with_existing_identity(tmp_path, monkeypatch, size):
    target = tmp_path / "report.bin"
    target.write_bytes(b"x" * size)
    backend = LocalStorageBackend(str(tmp_path))
    expected = await backend.get_version("report.bin")
    reads, hashes = [], []
    read, hash_bytes = local_snapshot.read_bounded, local_snapshot.content_hash_bytes

    def counted_read(stream, cap):
        reads.append(stream)
        return read(stream, cap)

    def counted_hash(data: bytes) -> str:
        hashes.append(len(data))
        return hash_bytes(data)

    monkeypatch.setattr(local_snapshot, "read_bounded", counted_read)
    monkeypatch.setattr(local_snapshot, "content_hash_bytes", counted_hash)

    if size > 8:
        with pytest.raises(StorageReadLimitExceededError):
            await backend.read_snapshot_bounded("report.bin", 8)
        assert reads == hashes == []
    else:
        snapshot = await backend.read_snapshot_bounded("report.bin", 8)
        assert snapshot.data == b"x" * size
        assert snapshot.sha256 == hashlib.sha256(b"x" * size).hexdigest()
        assert snapshot.version == expected
        assert hashes == [size]
        assert len(reads) == 1
        assert reads[0].closed


@pytest.mark.parametrize("change", ["replace", "overwrite", "delete"])
async def test_local_snapshot_rejects_detected_change_after_content_read(tmp_path, monkeypatch, change):
    target = tmp_path / "report.bin"
    target.write_bytes(b"original")
    before = target.stat()
    read = local_snapshot.read_bounded
    streams = []

    def changed_read(stream, cap):
        streams.append(stream)
        data = read(stream, cap)
        if change == "delete":
            target.unlink()
        elif change == "replace":
            replacement = tmp_path / "new"
            replacement.write_bytes(b"replaced")
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            replacement.replace(target)
        else:
            target.write_bytes(b"replaced")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return data

    monkeypatch.setattr(local_snapshot, "read_bounded", changed_read)

    with pytest.raises(StorageReadChangedError):
        await LocalStorageBackend(str(tmp_path)).read_snapshot_bounded("report.bin", 8)

    assert streams[0].closed


async def test_local_snapshot_growth_consumes_exactly_cap_plus_one_and_closes(tmp_path, monkeypatch):
    target = tmp_path / "growing.bin"
    target.write_bytes(b"ok")
    read = local_snapshot.read_bounded
    consumed = 0
    streams = []

    class GrowingReader:
        def __init__(self, stream: BoundedReader):
            self.stream = stream

        def read(self, size: int) -> bytes:
            nonlocal consumed
            chunk = self.stream.read(min(size, 2) if consumed == 0 else size)
            if consumed == 0:
                with target.open("ab", buffering=0) as writer:
                    writer.write(b"x" * 200_000)
            consumed += len(chunk)
            return chunk

    def growing_read(stream, cap):
        streams.append(stream)
        return read(GrowingReader(stream), cap)

    monkeypatch.setattr(local_snapshot, "read_bounded", growing_read)

    with pytest.raises(StorageReadLimitExceededError) as raised:
        await LocalStorageBackend(str(tmp_path)).read_snapshot_bounded("growing.bin", 8)

    assert (raised.value.size_bytes, consumed) == (9, 9)
    assert streams[0].closed


@pytest.mark.parametrize("outside", [False, True])
async def test_local_snapshot_rejects_symlink_components(tmp_path: Path, outside: bool):
    root = tmp_path / "root"
    root.mkdir()
    destination = tmp_path / "other" if outside else root / "real"
    destination.mkdir()
    (destination / "secret").write_bytes(b"secret")
    (root / "link").symlink_to(destination, target_is_directory=True)

    with pytest.raises(HTTPException) as raised:
        await LocalStorageBackend(str(root)).read_snapshot_bounded("link/secret", 8)

    assert raised.value.status_code == 403


async def test_snapshot_missing_and_unsupported_backends_fail_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        await LocalStorageBackend(str(tmp_path)).read_snapshot_bounded("missing", 8)
    with pytest.raises(NotImplementedError):
        await StorageBackend().read_snapshot_bounded("missing", 8)


async def test_local_snapshot_cancelled_awaiter_leaves_worker_owning_descriptor(tmp_path, monkeypatch):
    (tmp_path / "report").write_bytes(b"document")
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    open_file, read = local_snapshot.open_regular_file, local_snapshot.read_bounded
    streams = []

    @contextmanager
    def tracked_open(path):
        try:
            with open_file(path) as stream:
                streams.append(stream)
                yield stream
        finally:
            closed.set()

    def paused_read(stream, cap):
        entered.set()
        assert release.wait(5)
        return read(stream, cap)

    monkeypatch.setattr(local_snapshot, "open_regular_file", tracked_open)
    monkeypatch.setattr(local_snapshot, "read_bounded", paused_read)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(LocalStorageBackend(str(tmp_path)).read_snapshot_bounded, "report", 8)
            assert await anyio.to_thread.run_sync(entered.wait, 5)
            tasks.cancel_scope.cancel()
        assert not streams[0].closed
    finally:
        release.set()
        assert await anyio.to_thread.run_sync(closed.wait, 5)

    assert streams[0].closed
