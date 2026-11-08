import os
from pathlib import Path
from tempfile import mkstemp
from typing import override

import pytest

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    MaterializedFile,
    StorageBackend,
    StorageConditionalRaceError,
    StorageEntry,
    StorageReadLimitExceededError,
    WriteCondition,
    walk_order,
)
from app.services.storage_runtime.bounded import BoundedReader
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.quota_s3 import ManagedS3StorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend
from materialize_fixtures import temp_files, temp_root
from storage_walk_fixtures import (
    FILE_SIZE,
    ListOnlyBackend,
    PagedS3Client,
    build_tree_on_disk,
    s3_backend,
    shape,
)

__all__ = ["temp_root"]


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


async def test_fallback_storage_backfills_primary_on_read(tmp_path: Path):
    primary = MemoryStorageBackend()
    fallback = LocalStorageBackend(str(tmp_path))
    await fallback.write_bytes("agent-id/focus.md", b"# Focus\n\n- [ ] migrate me\n")
    storage = FallbackStorageBackend(primary=primary, fallback=fallback)

    content = await storage.read_text("agent-id/focus.md")

    assert "migrate me" in content
    assert primary.files["agent-id/focus.md"] == await fallback.read_bytes("agent-id/focus.md")


async def test_fallback_storage_writes_only_to_primary():
    primary = MemoryStorageBackend()
    fallback = MemoryStorageBackend()
    storage = FallbackStorageBackend(primary=primary, fallback=fallback)

    await storage.write_text("agent-id/focus.md", "# Focus\n")

    assert "agent-id/focus.md" in primary.files
    assert "agent-id/focus.md" not in fallback.files


def write_temp_copy(data: bytes) -> Path:
    descriptor, name = mkstemp()
    os.close(descriptor)
    copy = Path(name)
    _ = copy.write_bytes(data)
    return copy


class CopyingBackend(MemoryStorageBackend):
    """Materializes an owned temp copy, the way S3 does."""

    @override
    async def materialize(self, key: str) -> MaterializedFile | None:
        return MaterializedFile(write_temp_copy(self.files[key]), owned=True)


@pytest.mark.parametrize("unpromoted_legacy", [False, True])
async def test_fallback_materialize_never_removes_real_local_files(tmp_path: Path, temp_root: Path, unpromoted_legacy):
    # Given
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await (legacy if unpromoted_legacy else primary).write_bytes("report.txt", b"real")
    storage = FallbackStorageBackend(primary, legacy)
    # When
    handle = await storage.materialize("report.txt")
    assert handle is not None
    handle.close()
    # Then
    assert not handle.owned
    assert handle.path == tmp_path / "primary" / "report.txt"
    assert handle.path.read_bytes() == b"real"
    assert (tmp_path / "legacy" / "report.txt").exists() == unpromoted_legacy
    assert temp_files(temp_root) == []


async def test_fallback_materialize_passes_on_the_ownership_of_the_primary_winner(
    tmp_path: Path, temp_root: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given: an upload wins the primary key while the legacy file is being promoted
    primary = CopyingBackend()
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    await legacy.write_bytes("report.txt", b"old")
    original_read = legacy.read_bytes

    original_create = primary.create_file_if_absent

    async def raced_create(key: str, path: Path, content_type: str | None = None) -> ConditionalWriteResult:
        await primary.write_bytes(key, b"winning upload")
        return await original_create(key, path, content_type)

    monkeypatch.setattr(primary, "create_file_if_absent", raced_create)
    storage = FallbackStorageBackend(primary, legacy)
    # When
    handle = await storage.materialize("report.txt")
    # Then: the winner's copy is owned and removed, the legacy file is untouched
    assert handle is not None
    assert handle.owned
    assert handle.path.read_bytes() == b"winning upload"
    handle.close()
    assert temp_files(temp_root) == []
    assert await original_read("report.txt") == b"old"


@pytest.mark.parametrize("surface", ["read_bytes", "stat", "get_version", "local_path_for", "presign_download_url"])
async def test_fallback_promotion_returns_primary_when_upload_wins(
    surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    primary = LocalStorageBackend(str(tmp_path / "primary"))
    legacy = LocalStorageBackend(str(tmp_path / "legacy"))
    key = "report.txt"
    await legacy.write_bytes(key, b"old")
    original_read = legacy.read_bytes

    original_create = primary.create_file_if_absent

    async def raced_create(key: str, path: Path, content_type: str | None = None) -> ConditionalWriteResult:
        await primary.write_bytes(key, b"winning upload")
        return await original_create(key, path, content_type)

    async def presign(key: str, filename: str | None = None, inline: bool = False) -> str:
        return (await primary.read_bytes(key)).decode()

    monkeypatch.setattr(primary, "create_file_if_absent", raced_create)
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
async def test_fallback_promotion_bounds_retries_when_primary_keeps_disappearing(bounded, tmp_path: Path):
    class DisappearingPrimary(MemoryStorageBackend):
        attempts = 0

        @override
        async def create_bytes_if_absent(
            self, key: str, data: bytes, content_type: str | None = None
        ) -> ConditionalWriteResult:
            self.attempts += 1
            return ConditionalWriteResult(ok=False, conflict=True)

    primary = DisappearingPrimary()
    legacy = LocalStorageBackend(str(tmp_path))
    await legacy.write_bytes("report.txt", b"old")
    storage = FallbackStorageBackend(primary, legacy)

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


async def test_default_walk_over_a_list_only_backend_lists_one_directory_at_a_time(tmp_path):
    # Given
    build_tree_on_disk(tmp_path)
    backend = ListOnlyBackend(LocalStorageBackend(str(tmp_path)))
    # When
    walk = backend.walk("w")
    names = [(await anext(walk)).name for _ in range(4)]
    await walk.aclose()
    early_listings = list(backend.listed)
    backend.listed.clear()
    _ = [entry async for entry in backend.walk("w")]
    # Then
    assert names == ["B.txt", "a-b.txt", "a.txt", "a"]
    assert early_listings == ["w"]
    assert backend.listed == ["w", "w/a", "w/b", "w/b/deep", "w/empty"]


async def test_fallback_walk_merges_primary_and_legacy_in_one_deterministic_order(tmp_path, monkeypatch):
    # Given
    legacy_root = tmp_path / "legacy"
    for relative in ("w/a.txt", "w/b/old.txt", "w/.hidden/x"):
        (legacy_root / relative).parent.mkdir(parents=True, exist_ok=True)
        (legacy_root / relative).write_bytes(b"x" * FILE_SIZE)
    primary = s3_backend(PagedS3Client({"w/b/new.txt": FILE_SIZE, "w/c.txt": FILE_SIZE}), monkeypatch)
    backend = FallbackStorageBackend(primary, LocalStorageBackend(str(legacy_root)))
    # When
    entries = [entry async for entry in backend.walk("w")]
    # Then
    assert shape(entries) == [
        ("a.txt", "w/a.txt", False),
        ("b", "w/b", True),
        ("new.txt", "w/b/new.txt", False),
        ("old.txt", "w/b/old.txt", False),
        ("c.txt", "w/c.txt", False),
    ]


def merged_page_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FallbackStorageBackend, PagedS3Client]:
    """Primary and legacy overlap on a.txt and b.txt; legacy has a file "a" where the primary has a folder "a/"."""
    legacy_root = tmp_path / "legacy"
    for relative in ("w/a", "w/a.txt", "w/b.txt", "w/c.txt"):
        (legacy_root / relative).parent.mkdir(parents=True, exist_ok=True)
        (legacy_root / relative).write_bytes(b"x" * 3)
    client = PagedS3Client({"w/a.txt": 5, "w/a/new.txt": 5, "w/b.txt": 7, "w/d.txt": 5})
    backend = FallbackStorageBackend(s3_backend(client, monkeypatch), LocalStorageBackend(str(legacy_root)))
    return backend, client


@pytest.mark.parametrize("limit", [2, 3, 4, 20])
async def test_fallback_pages_merge_both_sides_without_duplicates_and_with_primary_winning(
    tmp_path, monkeypatch, limit
):
    # Given
    backend, _client = merged_page_fixture(tmp_path, monkeypatch)

    async def forbidden(key: str) -> list[StorageEntry]:
        raise AssertionError(f"full listing of {key}")

    monkeypatch.setattr(backend.primary, "list_dir", forbidden)
    monkeypatch.setattr(backend.fallback, "list_dir", forbidden)
    # When
    pages = []
    after = ""
    while True:
        page = await backend.list_dir_page("w", limit=limit, after=after)
        pages.append(page)
        if not page.has_more:
            break
        after = walk_order(page.entries[-1])
    # Then
    entries = [entry for page in pages for entry in page.entries]
    assert [(entry.name, entry.is_dir) for entry in entries] == [
        ("a", False),
        ("a.txt", False),
        ("a", True),
        ("b.txt", False),
        ("c.txt", False),
        ("d.txt", False),
    ]
    assert {entry.name: entry.size for entry in entries if not entry.is_dir and entry.name != "a"} == {
        "a.txt": 5,
        "b.txt": 7,
        "c.txt": 3,
        "d.txt": 5,
    }
    assert all(len(page.entries) <= limit for page in pages)
    assert all(page.has_more for page in pages[:-1])
    assert not pages[-1].has_more


async def test_fallback_page_resumes_both_sides_after_the_cursor(tmp_path, monkeypatch):
    # Given
    backend, client = merged_page_fixture(tmp_path, monkeypatch)
    scanned: list[tuple[str, int, str]] = []
    legacy = backend.fallback
    assert isinstance(legacy, LocalStorageBackend)
    scan = legacy._scan_page

    def spy(key: str, limit: int, after: str):
        scanned.append((key, limit, after))
        return scan(key, limit, after)

    monkeypatch.setattr(legacy, "_scan_page", spy)
    # When
    page = await backend.list_dir_page("w", limit=2, after="a/")
    # Then
    assert [walk_order(entry) for entry in page.entries] == ["b.txt", "c.txt"]
    assert page.has_more
    assert scanned == [("w", 2, "a/")]
    assert "w/a/" in str(client.list_calls[-1]["StartAfter"])


async def test_fallback_page_reports_more_when_only_one_side_continues(tmp_path, monkeypatch):
    # Given
    backend, _client = merged_page_fixture(tmp_path, monkeypatch)
    # When
    page = await backend.list_dir_page("w", limit=3, after="a.txt")
    # Then
    assert [walk_order(entry) for entry in page.entries] == ["a/", "b.txt", "c.txt"]
    assert page.has_more


def test_managed_s3_backend_inherits_the_native_page_without_an_override():
    assert "list_dir_page" not in vars(ManagedS3StorageBackend)
    assert ManagedS3StorageBackend.list_dir_page is S3StorageBackend.list_dir_page
