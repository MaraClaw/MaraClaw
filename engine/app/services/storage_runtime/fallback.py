"""Storage backend wrapper for gradual local-to-remote migration."""

from __future__ import annotations

from pathlib import Path
from typing import override

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageConditionalRaceError,
    StorageEntry,
    StorageReadLimitExceededError,
    StorageVersion,
    WriteCondition,
)


class FallbackStorageBackend(StorageBackend):
    """Read-through fallback backend.

    Writes go to the primary backend. Reads first try primary storage, then
    fallback storage; fallback hits are copied into primary storage so old local
    files are gradually migrated as they are used.
    """

    def __init__(self, primary: StorageBackend, fallback: StorageBackend):
        self.primary: StorageBackend = primary
        self.fallback: StorageBackend = fallback

    @override
    async def exists(self, key: str) -> bool:
        return await self.primary.exists(key) or await self.fallback.exists(key)

    @override
    async def is_file(self, key: str) -> bool:
        return await self.primary.is_file(key) or await self.fallback.is_file(key)

    @override
    async def is_dir(self, key: str) -> bool:
        return await self.primary.is_dir(key) or await self.fallback.is_dir(key)

    @override
    async def list_dir(self, key: str) -> list[StorageEntry]:
        entries_by_key: dict[str, StorageEntry] = {}
        for entry in await self.fallback.list_dir(key):
            entries_by_key[entry.key] = entry
        for entry in await self.primary.list_dir(key):
            entries_by_key[entry.key] = entry
        return sorted(entries_by_key.values(), key=lambda entry: (not entry.is_dir, entry.name))

    @override
    async def read_bytes(self, key: str) -> bytes:
        await self._promote_file(key)
        return await self.primary.read_bytes(key)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int) -> bytes:
        try:
            return await self.primary.read_bytes_bounded(key, max_bytes)
        except FileNotFoundError:
            pass
        try:
            data = await self.fallback.read_bytes_bounded(key, max_bytes)
        except (FileNotFoundError, StorageReadLimitExceededError) as legacy_error:
            try:
                return await self.primary.read_bytes_bounded(key, max_bytes)
            except FileNotFoundError:
                raise legacy_error from None
        for _attempt in range(3):
            await self.primary.create_bytes_if_absent(key, data)
            try:
                return await self.primary.read_bytes_bounded(key, max_bytes)
            except FileNotFoundError:
                pass
        raise StorageConditionalRaceError(key, 3)

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await self.primary.write_bytes(key, data, content_type=content_type)

    @override
    async def delete(self, key: str) -> None:
        await self.primary.delete(key)
        await self.fallback.delete(key)

    @override
    async def delete_tree(self, key: str) -> None:
        await self.primary.delete_tree(key)
        await self.fallback.delete_tree(key)

    @override
    async def stat(self, key: str) -> StorageEntry:
        if await self._promote_file(key):
            return await self.primary.stat(key)
        return await self.fallback.stat(key)

    @override
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        version = await self.primary.get_version_bounded(key, max_bytes)
        if version.exists:
            return version
        legacy = await self.fallback.get_version_bounded(key, max_bytes)
        if not legacy.exists or legacy.is_dir:
            return legacy
        await self.read_bytes_bounded(key, max_bytes)
        return await self.primary.get_version_bounded(key, max_bytes)

    @override
    async def get_version(self, key: str) -> StorageVersion:
        if await self._promote_file(key):
            return await self.primary.get_version(key)
        return await self.fallback.get_version(key)

    @override
    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if await self.fallback.exists(key):
            return ConditionalWriteResult(ok=False, conflict=True)
        return await self.primary.create_bytes_if_absent(key, data, content_type=content_type)

    @override
    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        if condition and condition.require_absent:
            return await super().write_bytes_if_match(key, data, condition=condition, content_type=content_type)
        return await self.primary.write_bytes_if_match(key, data, condition=condition, content_type=content_type)

    @override
    async def local_path_for(self, key: str) -> Path | None:
        if await self._promote_file(key):
            return await self.primary.local_path_for(key)
        return await self.fallback.local_path_for(key)

    @override
    async def presign_download_url(self, key: str, filename: str | None = None, inline: bool = False) -> str | None:
        await self._promote_file(key)
        return await self.primary.presign_download_url(key, filename=filename, inline=inline)

    async def _promote_file(self, key: str) -> bool:
        for _attempt in range(3):
            if await self.primary.exists(key):
                return True
            if not await self.fallback.is_file(key):
                return False
            data = await self.fallback.read_bytes(key)
            await self.primary.create_bytes_if_absent(key, data)
            if await self.primary.exists(key):
                return True
        raise StorageConditionalRaceError(key, 3)
