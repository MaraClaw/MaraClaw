from typing import override

from app.services.storage_runtime.base import StorageBackend, StorageEntry
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.s3 import S3StorageBackend


class MemoryStorageBackend(StorageBackend):
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})

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


async def test_s3_local_path_for_preserves_suffix_and_materialized_content():
    backend = object.__new__(S3StorageBackend)

    async def write_local_copy(key: str, path) -> None:
        path.write_bytes(key.encode())

    backend.write_local_copy = write_local_copy

    path = await backend.local_path_for("agent-id/report.txt")
    assert path is not None
    try:
        assert path.suffix == ".txt"
        assert path.read_bytes() == b"agent-id/report.txt"
    finally:
        path.unlink(missing_ok=True)
