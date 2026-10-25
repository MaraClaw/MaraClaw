"""Local filesystem storage backend."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast, override

import aiofiles
import anyio
from fastapi import HTTPException, status

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageReadLimitExceededError,
    StorageVersion,
    content_hash_bytes,
)
from app.services.storage_runtime.bounded import open_regular_file, read_bounded
from app.services.storage_runtime.utils import normalize_storage_key


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: str):
        self.root: Path = Path(root)

    def _full_path(self, key: str) -> Path:
        normalized = normalize_storage_key(key)
        full = (self.root / normalized).resolve()
        root_resolved = self.root.resolve()
        if not str(full).startswith(str(root_resolved)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal not allowed")
        return full

    @override
    async def exists(self, key: str) -> bool:
        return self._full_path(key).exists()

    @override
    async def is_file(self, key: str) -> bool:
        return self._full_path(key).is_file()

    @override
    async def is_dir(self, key: str) -> bool:
        return self._full_path(key).is_dir()

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        base = self._full_path(key)
        if not base.exists() or not base.is_dir():
            return []
        entries: list[StorageEntry] = []
        for entry in sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
            if entry.name == ".gitkeep" or entry.is_symlink():
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            try:
                rel = str(entry.resolve().relative_to(self.root.resolve()))
            except OSError, ValueError:
                continue
            entries.append(
                StorageEntry(
                    name=entry.name,
                    key=rel,
                    is_dir=entry.is_dir(),
                    size=stat.st_size if entry.is_file() else 0,
                    modified_at=str(stat.st_mtime),
                    version_id=_local_version_token(stat, None),
                )
            )
        return entries

    @override
    async def read_bytes(self, key: str) -> bytes:
        path = self._full_path(key)
        async with aiofiles.open(path, "rb") as f:
            return cast("bytes", await f.read())

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int) -> bytes:
        path = self.root.resolve() / normalize_storage_key(key)
        return await anyio.to_thread.run_sync(_read_file_bounded, path, max_bytes, abandon_on_cancel=True)

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._full_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "wb") as f:
            _ = await f.write(data)

    @override
    async def delete(self, key: str) -> None:
        path = self._full_path(key)
        if not path.exists():
            return
        if path.is_dir():
            await self.delete_tree(key)
        else:
            path.unlink()

    @override
    async def delete_tree(self, key: str) -> None:
        path = self._full_path(key)
        if not path.exists():
            return
        await asyncio.to_thread(_local_delete_tree, path)

    @override
    async def stat(self, key: str) -> StorageEntry:
        path = self._full_path(key)
        stat = path.stat()
        file_hash = ""
        version_id = _local_version_token(stat, None)
        if path.is_file():
            data = await self.read_bytes(key)
            file_hash = content_hash_bytes(data)
            version_id = _local_version_token(stat, file_hash)
        return StorageEntry(
            name=path.name,
            key=normalize_storage_key(key),
            is_dir=path.is_dir(),
            size=stat.st_size if path.is_file() else 0,
            modified_at=str(stat.st_mtime),
            version_id=version_id,
            etag=file_hash,
            content_hash=file_hash,
        )

    @override
    async def get_version(self, key: str) -> StorageVersion:
        return await self._get_version(key, None)

    @override
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        requested = self.root.resolve() / normalize_storage_key(key)
        if requested != requested.resolve():
            raise HTTPException(status_code=403, detail="Symlink previews are not allowed")
        return await self._get_version(key, max_bytes)

    async def _get_version(self, key: str, max_bytes: int | None) -> StorageVersion:
        path = self._full_path(key)
        if not path.exists():
            return StorageVersion(key=normalize_storage_key(key), exists=False, is_dir=False)
        stat = path.stat()
        if path.is_dir():
            return StorageVersion(
                key=normalize_storage_key(key),
                exists=True,
                is_dir=True,
                modified_at=str(stat.st_mtime),
                version_id=_local_version_token(stat, None),
            )
        if max_bytes is not None and stat.st_size > max_bytes:
            raise StorageReadLimitExceededError(max_bytes, stat.st_size)
        if max_bytes is not None and not path.is_file():
            raise FileNotFoundError(key)
        data = await self.read_bytes(key) if max_bytes is None else await self.read_bytes_bounded(key, max_bytes)
        file_hash = content_hash_bytes(data)
        return StorageVersion(
            key=normalize_storage_key(key),
            exists=True,
            is_dir=False,
            size=stat.st_size,
            modified_at=str(stat.st_mtime),
            etag=file_hash,
            version_id=_local_version_token(stat, file_hash),
            content_hash=file_hash,
        )

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        stat = await anyio.to_thread.run_sync(_publish_complete_file, self._full_path(key), data)
        if stat is None:
            return ConditionalWriteResult(ok=False, conflict=True)
        file_hash = content_hash_bytes(data)
        return ConditionalWriteResult(
            ok=True,
            current_version=StorageVersion(
                key=normalize_storage_key(key),
                exists=True,
                is_dir=False,
                size=len(data),
                modified_at=str(stat.st_mtime),
                etag=file_hash,
                version_id=_local_version_token(stat, file_hash),
                content_hash=file_hash,
            ),
        )

    @override
    async def local_path_for(self, key: str) -> Path | None:
        return self._full_path(key)


def _local_delete_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)


def _local_version_token(stat: os.stat_result, file_hash: str | None) -> str:
    hash_part = file_hash or ""
    return f"{stat.st_mtime_ns}:{stat.st_size}:{hash_part}"


def _read_file_bounded(path: Path, max_bytes: int) -> bytes:
    with open_regular_file(path) as stream:
        return read_bounded(stream, max_bytes)


def _publish_complete_file(path: Path, data: bytes) -> os.stat_result | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=".storage-create-", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            stat = os.fstat(temporary.fileno())
            temporary.close()
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                return None
            return stat
        finally:
            temporary_path.unlink(missing_ok=True)
