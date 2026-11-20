"""Local filesystem storage backend.

Every operation walks the normalized key from the resolved root through directory descriptors opened with O_NOFOLLOW
(`local_contained`, `local_create`, `bounded.open_regular_file`). A symlink anywhere below the root, planted before the
call or swapped in during it, is refused with `StorageLinkRefusedError` (403) and never followed, so a guest that writes
its own agent directory cannot redirect a member read or write elsewhere.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import override

import anyio

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    MaterializedFile,
    StorageBackend,
    StorageEntry,
    StoragePage,
    StorageSnapshot,
    StorageVersion,
    checked_page_limit,
    content_hash_bytes,
)
from app.services.storage_runtime.bounded import open_regular_file, read_bounded
from app.services.storage_runtime.local_contained import (
    local_version_token,
    lstat_entry,
    read_entry,
    read_version,
    regular_file_path,
    scan_directory,
    scan_page,
)
from app.services.storage_runtime.local_create import (
    delete_contained_entry,
    delete_contained_tree,
    publish_complete_file,
    write_contained_file,
)
from app.services.storage_runtime.local_snapshot import read_file_snapshot
from app.services.storage_runtime.owned_copy import materialize_copy
from app.services.storage_runtime.utils import normalize_storage_key


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str):
        self.root: Path = Path(root)

    async def _in_thread[T](self, operation: Callable[[Path, str], T], key: str, *, abandon: bool = True) -> T:
        """Run `operation(resolved root, normalized key)` in a worker; writes are not abandoned on cancel."""
        root, relative = self.root, normalize_storage_key(key)
        return await anyio.to_thread.run_sync(lambda: operation(root.resolve(), relative), abandon_on_cancel=abandon)

    async def _probe(self, key: str, accept: Callable[[int], bool]) -> bool:
        info = await self._in_thread(lstat_entry, key)
        return info is not None and accept(info.st_mode)

    @override
    async def exists(self, key: str) -> bool:
        return await self._probe(key, lambda _mode: True)

    @override
    async def is_file(self, key: str) -> bool:
        return await self._probe(key, stat.S_ISREG)

    @override
    async def is_dir(self, key: str) -> bool:
        return await self._probe(key, stat.S_ISDIR)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        entries = await anyio.to_thread.run_sync(self._scan_directory, key, abandon_on_cancel=True)
        return sorted(entries, key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def list_dir_page(self, key: str, *, limit: int, after: str = "") -> StoragePage:
        bounded = checked_page_limit(limit)
        return await anyio.to_thread.run_sync(self._scan_page, key, bounded, after, abandon_on_cancel=True)

    def _scan_page(self, key: str, limit: int, after: str) -> StoragePage:
        return scan_page(self.root.resolve(), normalize_storage_key(key), limit, after)

    def _scan_directory(self, key: str) -> list[StorageEntry]:
        return scan_directory(self.root.resolve(), normalize_storage_key(key))

    @override
    async def read_bytes(self, key: str) -> bytes:
        return await self._in_thread(_read_file, key)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        if offset is not None and offset < 0:
            raise ValueError("offset must be nonnegative")
        return await self._in_thread(lambda root, relative: _read_file_bounded(root / relative, max_bytes, offset), key)

    @override
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        if max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        return await self._in_thread(
            lambda root, relative: read_file_snapshot(root / relative, relative, max_bytes), key
        )

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await self._in_thread(lambda root, relative: write_contained_file(root, relative, data), key, abandon=False)

    @override
    async def delete(self, key: str) -> None:
        with suppress(FileNotFoundError):
            await self._in_thread(delete_contained_entry, key, abandon=False)

    @override
    async def delete_tree(self, key: str) -> None:
        await self._in_thread(delete_contained_tree, key, abandon=False)

    @override
    async def stat(self, key: str) -> StorageEntry:
        return await self._in_thread(read_entry, key)

    @override
    async def get_version(self, key: str) -> StorageVersion:
        return await self._in_thread(lambda root, relative: read_version(root, relative, None), key)

    @override
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        return await self._in_thread(lambda root, relative: read_version(root, relative, max_bytes), key)

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        info = await self._in_thread(
            lambda root, relative: publish_complete_file(root, relative, data), key, abandon=False
        )
        if info is None:
            return ConditionalWriteResult(ok=False, conflict=True)
        file_hash = content_hash_bytes(data)
        return ConditionalWriteResult(
            ok=True,
            current_version=StorageVersion(
                key=normalize_storage_key(key),
                exists=True,
                is_dir=False,
                size=len(data),
                modified_at=str(info.st_mtime),
                etag=file_hash,
                version_id=local_version_token(info, file_hash),
                content_hash=file_hash,
            ),
        )

    @override
    async def local_path_for(self, key: str) -> Path | None:
        """An existing regular file reached without links, else None; open it only through `open_regular_file`."""
        return await self._in_thread(regular_file_path, key)

    @override
    async def materialize(self, key: str) -> MaterializedFile:
        """An owned temp copy read from a contained descriptor, so no caller reopens the guest path by name."""
        root, relative = self.root, normalize_storage_key(key)
        return await materialize_copy(lambda: open_regular_file(root.resolve() / relative), Path(relative).suffix)


def _read_file(root: Path, relative: str) -> bytes:
    with open_regular_file(root / relative) as stream:
        return stream.readall()


def _read_file_bounded(path: Path, max_bytes: int, offset: int | None = None) -> bytes:
    with open_regular_file(path) as stream:
        if offset is not None:
            _ = stream.seek(offset)
            return read_bounded(stream, max_bytes, range_read=True)
        return read_bounded(stream, max_bytes)
