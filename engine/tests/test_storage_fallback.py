from pathlib import Path
from typing import override

import pytest

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageConditionalRaceError,
    StorageEntry,
    StorageReadLimitExceededError,
    WriteCondition,
)
from app.services.storage_runtime.bounded import BoundedReader
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend


class MemoryStorageBackend(StorageBackend):
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files: dict[str, bytes] = dict(files or {})

    @override
    async def exists(self, key: str) -> bool:
        return key in self.files

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.files

    @override
    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.files)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries = []
        for existing, data in self.files.items():
            if existing.startswith(prefix):
                name = existing.removeprefix(prefix).split("/", 1)[0]
                entries.append(StorageEntry(name=name, key=f"{prefix}{name}", is_dir=False, size=len(data)))
        return entries

    @override
    async def read_bytes(self, key: str) -> bytes:
        if key not in self.files:
            raise FileNotFoundError(key)
        return self.files[key]

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int) -> bytes:
        if key not in self.files:
            raise FileNotFoundError(key)
        data = self.files[key][: max_bytes + 1]
        if len(data) > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, len(data))
        return data

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if key in self.files:
            return ConditionalWriteResult(ok=False, conflict=True)
        self.files[key] = data
        return ConditionalWriteResult(ok=True)

    @override
    async def delete(self, key: str) -> None:
        self.files.pop(key, None)

    @override
    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.files):
            if existing.startswith(prefix):
                self.files.pop(existing, None)

    @override
    async def stat(self, key: str) -> StorageEntry:
        if key not in self.files:
            raise FileNotFoundError(key)
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.files[key]))


async def test_fallback_storage_backfills_primary_on_read():
    primary = MemoryStorageBackend()
    fallback = MemoryStorageBackend({"agent-id/focus.md": b"# Focus\n\n- [ ] migrate me\n"})
    storage = FallbackStorageBackend(primary=primary, fallback=fallback)

    content = await storage.read_text("agent-id/focus.md")

    assert "migrate me" in content
    assert primary.files["agent-id/focus.md"] == fallback.files["agent-id/focus.md"]


async def test_fallback_storage_writes_only_to_primary():
    primary = MemoryStorageBackend()
    fallback = MemoryStorageBackend()
    storage = FallbackStorageBackend(primary=primary, fallback=fallback)

    await storage.write_text("agent-id/focus.md", "# Focus\n")

    assert "agent-id/focus.md" in primary.files
    assert "agent-id/focus.md" not in fallback.files


async def test_s3_local_path_for_preserves_suffix_and_materialized_content(monkeypatch: pytest.MonkeyPatch):
    backend = object.__new__(S3StorageBackend)

    async def write_local_copy(key: str, path) -> None:
        path.write_bytes(key.encode())

    monkeypatch.setattr(backend, "write_local_copy", write_local_copy)

    path = await backend.local_path_for("agent-id/report.txt")
    assert path is not None
    try:
        assert path.suffix == ".txt"
        assert path.read_bytes() == b"agent-id/report.txt"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.parametrize("surface", ["read_bytes", "stat", "get_version", "local_path_for", "presign_download_url"])
async def test_fallback_promotion_returns_primary_when_upload_wins(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    key = "report.txt"
    await legacy.write_bytes(key, b"old")
    original_read = legacy.read_bytes

    async def raced_read(key: str) -> bytes:
        data = await original_read(key)
        await primary.write_bytes(key, b"winning upload")
        return data

    async def presign(key: str, filename: str | None = None, inline: bool = False) -> str:
        return (await primary.read_bytes(key)).decode()

    monkeypatch.setattr(legacy, "read_bytes", raced_read)
    monkeypatch.setattr(primary, "presign_download_url", presign)
    storage = FallbackStorageBackend(primary, legacy)

    result = await getattr(storage, surface)(key)

    assert await primary.read_bytes(key) == b"winning upload"
    if surface == "read_bytes":
        assert result == b"winning upload"
    elif surface in {"stat", "get_version"}:
        assert result.size == len(b"winning upload")
    elif surface == "local_path_for":
        assert result == tmp_path / "primary" / key
        assert result.read_bytes() == b"winning upload"
    else:
        assert result == "winning upload"
    assert await original_read(key) == b"old"


@pytest.mark.parametrize("via_condition", [False, True])
async def test_fallback_create_treats_legacy_only_object_as_occupied(via_condition: bool):
    primary = MemoryStorageBackend()
    legacy = MemoryStorageBackend({"report.txt": b"old"})
    storage = FallbackStorageBackend(primary, legacy)

    if via_condition:
        result = await storage.write_bytes_if_match("report.txt", b"new", condition=WriteCondition(require_absent=True))
    else:
        result = await storage.create_bytes_if_absent("report.txt", b"new")

    assert result.conflict
    assert not result.ok
    assert primary.files == {}
    assert legacy.files == {"report.txt": b"old"}


async def test_fallback_create_only_creates_new_key_in_primary():
    primary = MemoryStorageBackend()
    legacy = MemoryStorageBackend()
    storage = FallbackStorageBackend(primary, legacy)

    result = await storage.create_bytes_if_absent("report.txt", b"new")

    assert result.ok
    assert primary.files == {"report.txt": b"new"}
    assert legacy.files == {}


@pytest.mark.parametrize("bounded", [False, True])
async def test_fallback_promotion_bounds_retries_when_primary_keeps_disappearing(bounded):
    class DisappearingPrimary(MemoryStorageBackend):
        attempts = 0

        @override
        async def create_bytes_if_absent(
            self, key: str, data: bytes, content_type: str | None = None
        ) -> ConditionalWriteResult:
            self.attempts += 1
            return ConditionalWriteResult(ok=False, conflict=True)

    primary = DisappearingPrimary()
    storage = FallbackStorageBackend(primary, MemoryStorageBackend({"report.txt": b"old"}))

    operation = storage.read_bytes_bounded("report.txt", 8) if bounded else storage.read_bytes("report.txt")
    with pytest.raises(StorageConditionalRaceError):
        await operation

    assert primary.attempts == 3


@pytest.mark.parametrize("winner_size", [3, 200_000])
@pytest.mark.parametrize("winner_timing", ["present", "during_promotion"])
async def test_bounded_fallback_applies_limit_to_primary_winner_without_hidden_reads(
    tmp_path, monkeypatch, winner_size, winner_timing
):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.bin", b"old")
    original_read = legacy.read_bytes_bounded
    reads: list[str] = []

    async def raced_read(key: str, max_bytes: int) -> bytes:
        reads.append(key)
        data = await original_read(key, max_bytes)
        await primary.write_bytes(key, b"w" * winner_size)
        return data

    async def forbidden(*_args):
        pytest.fail("bounded promotion must not use ordinary reads, stat or version lookup")

    if winner_timing == "present":
        await primary.write_bytes("report.bin", b"w" * winner_size)
    monkeypatch.setattr(legacy, "read_bytes_bounded", raced_read)
    for backend in (primary, legacy):
        for operation in ("read_bytes", "get_version", "stat"):
            monkeypatch.setattr(backend, operation, forbidden)
    storage = FallbackStorageBackend(primary, legacy)

    if winner_size > 8:
        with pytest.raises(StorageReadLimitExceededError) as raised:
            await storage.read_bytes_bounded("report.bin", 8)
        assert raised.value.size_bytes == 9
    else:
        assert await storage.read_bytes_bounded("report.bin", 8) == b"w" * winner_size

    assert reads == ([] if winner_timing == "present" else ["report.bin"])
    assert (tmp_path / "primary/report.bin").stat().st_size == winner_size
    assert (tmp_path / "legacy/report.bin").read_bytes() == b"old"


async def test_bounded_fallback_promotes_real_file_without_metadata_reread(tmp_path, monkeypatch):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.bin", b"document")

    async def forbidden(*_args):
        pytest.fail("bounded promotion must not trigger a full read/hash")

    for backend in (primary, legacy):
        for operation in ("read_bytes", "get_version", "stat"):
            monkeypatch.setattr(backend, operation, forbidden)

    assert await FallbackStorageBackend(primary, legacy).read_bytes_bounded("report.bin", 8) == b"document"

    assert (tmp_path / "primary/report.bin").read_bytes() == b"document"
    assert (tmp_path / "legacy/report.bin").read_bytes() == b"document"


async def test_bounded_fallback_never_promotes_oversized_legacy(tmp_path, monkeypatch):
    from app.services.storage_runtime import local

    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.bin", b"x" * 200_000)
    original = local.read_bounded
    consumed = 0

    class Reader:
        def __init__(self, stream: BoundedReader):
            self.stream = stream

        def read(self, size: int) -> bytes:
            nonlocal consumed
            chunk = self.stream.read(size)
            consumed += len(chunk)
            return chunk

    def observe(stream, max_bytes):
        return original(Reader(stream), max_bytes)

    monkeypatch.setattr(local, "read_bounded", observe)

    with pytest.raises(StorageReadLimitExceededError):
        await FallbackStorageBackend(primary, legacy).read_bytes_bounded("report.bin", 8)

    assert consumed == 9
    assert not (tmp_path / "primary").exists()
    assert (tmp_path / "legacy/report.bin").stat().st_size == 200_000


async def test_bounded_fallback_primary_wins_when_legacy_overflows_during_upload(tmp_path, monkeypatch):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.bin", b"x" * 200_000)
    original = legacy.read_bytes_bounded

    async def raced_read(key: str, max_bytes: int) -> bytes:
        try:
            return await original(key, max_bytes)
        finally:
            await primary.write_bytes(key, b"winner")

    monkeypatch.setattr(legacy, "read_bytes_bounded", raced_read)

    assert await FallbackStorageBackend(primary, legacy).read_bytes_bounded("report.bin", 8) == b"winner"

    assert (tmp_path / "primary/report.bin").read_bytes() == b"winner"
    assert (tmp_path / "legacy/report.bin").stat().st_size == 200_000


@pytest.mark.parametrize("winner_size", [3, 200_000])
async def test_bounded_fallback_primary_wins_when_legacy_is_missing_during_upload(tmp_path, monkeypatch, winner_size):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    original = legacy.read_bytes_bounded

    async def raced_read(key: str, max_bytes: int) -> bytes:
        try:
            return await original(key, max_bytes)
        finally:
            await primary.write_bytes(key, b"w" * winner_size)

    monkeypatch.setattr(legacy, "read_bytes_bounded", raced_read)
    storage = FallbackStorageBackend(primary, legacy)

    if winner_size > 8:
        with pytest.raises(StorageReadLimitExceededError) as raised:
            await storage.read_bytes_bounded("report.bin", 8)
        assert raised.value.size_bytes == 9
    else:
        assert await storage.read_bytes_bounded("report.bin", 8) == b"w" * winner_size

    assert not (tmp_path / "legacy").exists()
    assert (tmp_path / "primary/report.bin").stat().st_size == winner_size
