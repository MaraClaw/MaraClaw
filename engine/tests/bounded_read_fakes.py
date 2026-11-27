"""A storage stand-in that serves only bounded reads, for code that must never load a whole file.

`RecordingStorage` keeps files as bytes and answers `read_bytes_bounded` like the real backends: with `offset` it returns
a range, without it a file over `max_bytes` raises `StorageReadLimitExceededError`. A whole-file `read_bytes` (and so
`read_text`) is recorded in `whole_reads` and fails, so a test sees the read whether or not the code under test swallows
the error.
"""

from typing import override

from app.services.storage_runtime.base import StorageBackend, StorageEntry, StorageReadLimitExceededError


class RecordingStorage(StorageBackend):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files: dict[str, bytes] = files
        self.reads: list[tuple[str, int, int | None]] = []
        self.whole_reads: list[str] = []

    @override
    async def exists(self, key: str) -> bool:
        return key in self.files or await self.is_dir(key)

    @override
    async def is_file(self, key: str) -> bool:
        return key in self.files

    @override
    async def is_dir(self, key: str) -> bool:
        return any(path.startswith(f"{key}/") for path in self.files)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        entries: dict[str, StorageEntry] = {}
        for path, data in self.files.items():
            if not path.startswith(f"{key}/"):
                continue
            rest = path.removeprefix(f"{key}/")
            name = rest.split("/")[0]
            child = f"{key}/{name}"
            entries[name] = StorageEntry(name=name, key=child, is_dir="/" in rest, size=0 if "/" in rest else len(data))
        return sorted(entries.values(), key=lambda entry: entry.name)

    @override
    async def stat(self, key: str) -> StorageEntry:
        if key not in self.files:
            raise FileNotFoundError(key)
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.files[key]))

    @override
    async def read_bytes(self, key: str) -> bytes:
        self.whole_reads.append(key)
        raise AssertionError(f"whole-file read of {key}")

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        self.reads.append((key, max_bytes, offset))
        if key not in self.files:
            raise FileNotFoundError(key)
        data = self.files[key]
        if offset is not None:
            return data[offset : offset + max_bytes]
        if len(data) > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, max_bytes + 1)
        return data
