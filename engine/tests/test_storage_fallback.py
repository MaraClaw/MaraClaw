from pathlib import Path
from typing import override

import pytest

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageConditionalRaceError,
    StorageEntry,
    WriteCondition,
)
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


async def test_fallback_promotion_bounds_retries_when_primary_keeps_disappearing():
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

    with pytest.raises(StorageConditionalRaceError):
        await storage.read_bytes("report.txt")

    assert primary.attempts == 3
