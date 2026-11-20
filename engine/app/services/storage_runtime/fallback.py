"""Storage backend wrapper for gradual local-to-remote migration."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from heapq import merge
from pathlib import Path
from typing import BinaryIO, override

from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    MaterializedFile,
    StorageBackend,
    StorageConditionalRaceError,
    StorageEntry,
    StoragePage,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    WriteCondition,
    walk_order,
)


class FallbackStorageBackend(StorageBackend):
    """Read-through fallback backend.

    Writes go to the primary backend. Reads first try primary storage, then
    fallback storage; fallback hits are copied into primary storage so old local
    files are gradually migrated as they are used.

    With ``promote=False`` nothing is ever copied into the primary: a key only the fallback holds is served
    read-only from it, and a conditional write to such a key creates the primary object without copying the file.
    """

    def __init__(self, primary: StorageBackend, fallback: StorageBackend, promote: bool = True):
        self.primary: StorageBackend = primary
        self.fallback: StorageBackend = fallback
        self.promote: bool = promote

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
    async def list_dir_page(self, key: str, *, limit: int, after: str = "") -> StoragePage:
        # Each side resumes after the same cursor, so the first ``limit`` merged children come from the first ``limit``
        # of each side; a stable merge over (primary, legacy) lets the primary win an equal (key, is_dir).
        primary = await self.primary.list_dir_page(key, limit=limit, after=after)
        legacy = await self.fallback.list_dir_page(key, limit=limit, after=after)
        merged: list[StorageEntry] = []
        seen: set[tuple[str, bool]] = set()
        for entry in merge(primary.entries, legacy.entries, key=walk_order):
            if (entry.key, entry.is_dir) not in seen:
                seen.add((entry.key, entry.is_dir))
                merged.append(entry)
        return StoragePage(merged[:limit], len(merged) > limit or primary.has_more or legacy.has_more)

    @override
    def list_files_flat(self, prefix: str) -> AsyncGenerator[StorageEntry]:
        return self.primary.list_files_flat(prefix)

    @override
    async def read_bytes(self, key: str) -> bytes:
        if not self.promote:
            try:
                return await self.primary.read_bytes(key)
            except FileNotFoundError:
                return await self.fallback.read_bytes(key)
        await self._promote_file(key)
        return await self.primary.read_bytes(key)

    @override
    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        if offset is not None or not self.promote:
            # A partial object cannot be promoted as a complete primary winner, and an unpromoted read copies
            # nothing.
            try:
                return await self.primary.read_bytes_bounded(key, max_bytes, offset=offset)
            except FileNotFoundError:
                return await self.fallback.read_bytes_bounded(key, max_bytes, offset=offset)
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
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        try:
            return await self.primary.read_snapshot_bounded(key, max_bytes)
        except FileNotFoundError:
            if not self.promote:
                return await self.fallback.read_snapshot_bounded(key, max_bytes)
        try:
            legacy = await self.fallback.read_snapshot_bounded(key, max_bytes)
        except (FileNotFoundError, StorageReadLimitExceededError) as legacy_error:
            try:
                return await self.primary.read_snapshot_bounded(key, max_bytes)
            except FileNotFoundError:
                raise legacy_error from None
        for _attempt in range(3):
            _ = await self.primary.create_bytes_if_absent(key, legacy.data)
            try:
                return await self.primary.read_snapshot_bounded(key, max_bytes)
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
    async def remove_empty_directory(self, key: str) -> None:
        # Writes land in the primary, so only directories the primary holds can be this backend's leftovers.
        await self.primary.remove_empty_directory(key)

    @override
    async def stat(self, key: str) -> StorageEntry:
        if await self._primary_serves(key):
            return await self.primary.stat(key)
        return await self.fallback.stat(key)

    @override
    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        version = await self.primary.get_version_bounded(key, max_bytes)
        if version.exists:
            return version
        legacy = await self.fallback.get_version_bounded(key, max_bytes)
        if not self.promote or not legacy.exists or legacy.is_dir:
            return legacy
        await self.read_bytes_bounded(key, max_bytes)
        return await self.primary.get_version_bounded(key, max_bytes)

    @override
    async def get_version(self, key: str) -> StorageVersion:
        if await self._primary_serves(key):
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
    async def create_file_if_absent(
        self, key: str, path: Path, content_type: str | None = None
    ) -> ConditionalWriteResult:
        if await self.fallback.exists(key):
            return ConditionalWriteResult(ok=False, conflict=True)
        return await self.primary.create_file_if_absent(key, path, content_type=content_type)

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
        if not self.promote and condition and condition.version_token is not None and await self._local_only(key):
            # The caller edited the legacy file it saw: publish the primary object, never copy the file first.
            legacy = await self.fallback.get_version(key)
            if legacy.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=legacy)
            return await self.primary.create_bytes_if_absent(key, data, content_type=content_type)
        return await self.primary.write_bytes_if_match(key, data, condition=condition, content_type=content_type)

    @override
    async def delete_if_match(self, key: str, *, condition: WriteCondition | None = None) -> ConditionalWriteResult:
        if self.promote:
            await self._promote_file(key)
        elif await self._local_only(key):
            return await super().delete_if_match(key, condition=condition)
        result = await self.primary.delete_if_match(key, condition=condition)
        if result.ok:
            await self.fallback.delete(key)
        return result

    @override
    async def open_local_file(self, key: str) -> tuple[BinaryIO, os.stat_result] | None:
        if await self._primary_serves(key):
            return await self.primary.open_local_file(key)
        return await self.fallback.open_local_file(key)

    @override
    async def materialize(self, key: str) -> MaterializedFile | None:
        # Ownership is whatever the backend that produced the file reports: the primary winner after promotion
        # (a temporary S3 copy is owned), otherwise the legacy backend's real file, which is never owned.
        if await self._primary_serves(key):
            return await self.primary.materialize(key)
        return await self.fallback.materialize(key)

    @override
    async def presign_download_url(
        self, key: str, filename: str | None = None, inline: bool = False, expires_in_seconds: int | None = None
    ) -> str | None:
        if self.promote:
            await self._promote_file(key)
        elif await self._local_only(key):
            return None
        return await self.primary.presign_download_url(
            key, filename=filename, inline=inline, expires_in_seconds=expires_in_seconds
        )

    async def _primary_serves(self, key: str) -> bool:
        """Whether the primary holds the key, after copying a legacy file into it unless promotion is off."""
        return await self._promote_file(key) if self.promote else await self.primary.exists(key)

    async def _local_only(self, key: str) -> bool:
        """Whether the fallback holds the key as a file and the primary does not."""
        return not await self.primary.exists(key) and await self.fallback.is_file(key)

    async def _promote_file(self, key: str) -> bool:
        for _attempt in range(3):
            if await self.primary.exists(key):
                return True
            if not await self.fallback.is_file(key):
                return False
            # Publish from a copy the fallback read through its own contained descriptor, never its path by name.
            legacy = await self.fallback.materialize(key)
            if legacy is None:
                raise RuntimeError(f"Fallback file has no local copy: {key}")
            try:
                _ = await self.primary.create_file_if_absent(key, legacy.path)
            finally:
                legacy.close()
            if await self.primary.exists(key):
                return True
        raise StorageConditionalRaceError(key, 3)
