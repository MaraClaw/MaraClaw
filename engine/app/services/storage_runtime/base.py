"""Base storage types and interfaces."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import anyio
from fastapi import HTTPException, status

from app.services.storage_runtime.upload_source import UploadSource


@dataclass
class StorageEntry:
    name: str
    key: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""
    etag: str = ""
    version_id: str = ""
    content_hash: str = ""

    def version(self) -> StorageVersion:
        """The version this listing row names, so a caller can hold it without a second request per key."""
        return StorageVersion(
            key=self.key,
            exists=True,
            is_dir=self.is_dir,
            size=self.size,
            modified_at=self.modified_at,
            etag=self.etag,
            version_id=self.version_id,
            content_hash=self.content_hash,
        )


@dataclass
class StoragePage:
    """One bounded slice of a directory in ``walk_order``; ``has_more`` means children sort after the last entry."""

    entries: list[StorageEntry]
    has_more: bool


@dataclass
class StorageVersion:
    key: str
    exists: bool
    is_dir: bool
    size: int = 0
    modified_at: str = ""
    etag: str = ""
    version_id: str = ""
    content_hash: str = ""

    @property
    def token(self) -> str:
        return self.version_id or self.etag or self.content_hash or f"{self.modified_at}:{self.size}"


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """Bytes and storage identity from one read, with a separate actual SHA-256 digest."""

    data: bytes
    version: StorageVersion
    sha256: str


@dataclass
class WriteCondition:
    version_token: str | None = None
    require_absent: bool = False


@dataclass
class ConditionalWriteResult:
    ok: bool
    conflict: bool = False
    current_version: StorageVersion | None = None


def deleted_result(key: str) -> ConditionalWriteResult:
    """A batch delete's answer for a key that is now gone."""
    return ConditionalWriteResult(ok=True, current_version=StorageVersion(key=key, exists=False, is_dir=False))


def kept_result(key: str, *, exists: bool) -> ConditionalWriteResult:
    """A batch answer for a key left as it was: its version changed (``exists``), or nothing was there to change."""
    return ConditionalWriteResult(
        ok=False, conflict=True, current_version=StorageVersion(key=key, exists=exists, is_dir=False)
    )


BATCH_KEY_LIMIT = 1000
"""The most keys one batch call takes: the ``DeleteObjects`` limit, which sizes every caller's chunks too."""


@dataclass(frozen=True, slots=True)
class CopyRequest:
    """Copy ``source`` to ``destination_key`` while the source still holds the version it was listed with."""

    source: StorageVersion
    destination_key: str


@dataclass(frozen=True, slots=True)
class MaterializedFile:
    """A local file holding one storage key, and whether the holder must remove it.

    ``owned`` is true only for a temporary copy made for the caller. A real workspace file or an unpromoted legacy
    file is never owned, so ``close`` never unlinks it.
    """

    path: Path
    owned: bool = False

    def close(self) -> None:
        if self.owned:
            self.path.unlink(missing_ok=True)


class AtomicCreateUnsupportedError(NotImplementedError):
    def __init__(self, backend: str, key: str):
        self.backend: str = backend
        self.key: str = key
        super().__init__(f"Atomic creation is not supported by {backend}: {key}")


class StorageConditionalRaceError(RuntimeError):
    def __init__(self, key: str, attempts: int):
        self.key: str = key
        self.attempts: int = attempts
        super().__init__(f"Conditional storage race persisted after {attempts} attempts: {key}")


class StorageDeleteFailedError(RuntimeError):
    def __init__(self, key: str, failed: int):
        self.key: str = key
        self.failed: int = failed
        super().__init__(f"{failed} objects under {key or 'the storage root'} could not be deleted")


class StorageReadLimitExceededError(ValueError):
    def __init__(self, max_bytes: int, size_bytes: int):
        self.max_bytes: int = max_bytes
        self.size_bytes: int = size_bytes
        super().__init__(f"Storage read exceeds {max_bytes} bytes (observed {size_bytes})")


class StorageReadChangedError(OSError):
    def __init__(self, key: str):
        self.key: str = key
        super().__init__(f"Storage file changed while reading: {key}")


class StorageLinkRefusedError(HTTPException):
    """A local storage path reaches a symlink below the root, which is refused instead of followed (403)."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail="Path traversal not allowed")


MAX_DIR_PAGE_LIMIT = 500


def walk_order(entry: StorageEntry) -> str:
    """Sibling sort key that makes a depth-first walk follow the byte order of full object keys."""
    return f"{entry.name}/" if entry.is_dir else entry.name


def checked_page_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_DIR_PAGE_LIMIT:
        raise ValueError(f"Directory page limit must be between 1 and {MAX_DIR_PAGE_LIMIT}, got {limit}")
    return limit


async def walk_tree(list_dir: Callable[[str], Awaitable[list[StorageEntry]]], key: str) -> AsyncGenerator[StorageEntry]:
    """Lazily walk ``key`` with one ``list_dir`` call per directory, in the order ``StorageBackend.walk`` documents."""
    pending: list[Iterator[StorageEntry]] = [_walk_children(await list_dir(key))]
    while pending:
        entry = next(pending[-1], None)
        if entry is None:
            _ = pending.pop()
            continue
        yield entry
        if entry.is_dir:
            pending.append(_walk_children(await list_dir(entry.key)))


def _walk_children(entries: list[StorageEntry]) -> Iterator[StorageEntry]:
    return iter(sorted((entry for entry in entries if not entry.name.startswith(".")), key=walk_order))


class StorageBackend:
    async def exists(self, key: str) -> bool:
        raise NotImplementedError

    async def is_file(self, key: str) -> bool:
        raise NotImplementedError

    async def is_dir(self, key: str) -> bool:
        raise NotImplementedError

    async def list_dir(self, key: str) -> list[StorageEntry]:
        raise NotImplementedError

    async def list_dir_page(self, key: str, *, limit: int, after: str = "") -> StoragePage:
        """Return at most ``limit`` children of ``key`` sorting after ``after``, in ``walk_order``.

        ``after`` is the ``walk_order`` of the last child already consumed (empty to start), so a folder ``a`` resumes
        as ``"a/"`` and never repeats. Children are not filtered, hidden names included. Backends that implement only
        ``list_dir`` inherit this default, which drains the whole listing; native backends bound the work to the page.
        """
        bounded = checked_page_limit(limit)
        later = sorted((entry for entry in await self.list_dir(key) if walk_order(entry) > after), key=walk_order)
        return StoragePage(later[:bounded], len(later) > bounded)

    def walk(self, key: str) -> AsyncGenerator[StorageEntry]:
        """Lazily yield every descendant of ``key``; callers that stop early should ``aclose()`` the generator.

        Order is the byte order of full object keys (the order a flat S3 listing returns): siblings sort by
        ``walk_order``, so a directory ``a`` falls between ``a-b`` and ``a0``, and each directory entry is yielded
        immediately before its contents. Names starting with ``.`` are never yielded and their subtrees are never
        entered. Backends that implement only ``list_dir`` inherit this walk, one listing per directory.
        """
        return walk_tree(self.list_dir, key)

    def list_files_flat(self, prefix: str) -> AsyncGenerator[StorageEntry]:
        """Lazily yield every file under ``prefix`` in key order, from a listing with no delimiter.

        Unlike ``walk`` this yields no directories and skips no names, hidden ones included, so a caller comparing
        successive listings sees every object once with its size and etag. Backends that cannot list flat fail closed.
        """
        del prefix
        raise NotImplementedError

    async def read_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    async def read_bytes_bounded(self, key: str, max_bytes: int, *, offset: int | None = None) -> bytes:
        """Read at most max_bytes, consuming only one additional byte to detect overflow.

        Raise StorageReadLimitExceededError on actual-byte overflow and FileNotFoundError
        for an absent key. Implementations must not first hash, promote, or fully
        read the object. A cancelled worker retains ownership of its resources
        until its bounded blocking operation finishes and closes them.

        With ``offset`` supplied, return a range of at most ``max_bytes`` starting
        at that nonnegative byte offset, without overflow probing or promotion.
        A short result means EOF; an offset at/past EOF returns empty bytes.
        Omit ``offset`` to retain the original whole-object overflow contract.
        """
        raise NotImplementedError

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        raw = await self.read_bytes(key)
        return raw.decode(encoding, errors=errors)

    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        """Read bounded bytes, their storage identity and actual SHA-256 together.

        Reject detected local changes. A remote identity must come from the GET
        supplying the bytes, never a separate probe. Keep existing token semantics
        and worker ownership through cancellation; unsupported backends fail closed.
        """
        raise NotImplementedError

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        raise NotImplementedError

    async def create_bytes_if_absent(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> ConditionalWriteResult:
        """Publish complete bytes without replacement, or report an occupied key.

        Backends must implement a native atomic primitive. Capability, provider,
        and transport failures raise; they must never become absence/conflicts.
        """
        del data, content_type
        raise AtomicCreateUnsupportedError(type(self).__name__, key)

    async def create_file_if_absent(
        self, key: str, path: Path, content_type: str | None = None
    ) -> ConditionalWriteResult:
        """Publish a local file's bytes without replacement, with ``create_bytes_if_absent`` outcomes.

        This default reads the whole file into memory; backends that can stream the file override it.
        """
        data = await anyio.to_thread.run_sync(path.read_bytes)
        return await self.create_bytes_if_absent(key, data, content_type)

    async def write_source_if_match(
        self,
        key: str,
        source: UploadSource,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        """Stream an open local file's first ``source.size`` bytes with ``write_bytes_if_match`` outcomes.

        ``require_absent`` creates the key and reports an occupied one as a conflict; a ``version_token`` overwrites
        only while the key still holds that version; no condition overwrites. The body is seekable with a known length,
        so a retry rewinds it. The caller opened ``source`` and closes it, and may send it again after a conflict.
        This default reads the whole file into memory; backends that can stream it override this.
        """
        data = await anyio.to_thread.run_sync(source.body().read)
        return await self.write_bytes_if_match(key, data, condition=condition, content_type=content_type)

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        await self.write_bytes(key, content.encode(encoding), content_type="text/plain; charset=utf-8")

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def delete_tree(self, key: str) -> None:
        raise NotImplementedError

    async def remove_empty_directory(self, key: str) -> None:
        """Remove the directory at ``key`` only while it is empty; stores without real directories have none."""
        del key

    async def stat(self, key: str) -> StorageEntry:
        raise NotImplementedError

    async def get_version(self, key: str) -> StorageVersion:
        try:
            entry = await self.stat(key)
        except FileNotFoundError:
            return StorageVersion(key=key, exists=False, is_dir=False)
        return entry.version()

    async def get_version_bounded(self, key: str, max_bytes: int) -> StorageVersion:
        """Resolve the normal version identity without unbounded hashing/promotion.

        Reject oversized files before reading, and bound any identity reads or
        fallback promotion by actual bytes. Unsupported backends fail closed.
        """
        raise NotImplementedError

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        if condition and condition.require_absent:
            if condition.version_token is not None:
                current = await self.get_version(key)
                if current.token != condition.version_token:
                    return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
            return await self.create_bytes_if_absent(key, data, content_type=content_type)
        current = await self.get_version(key)
        if condition and condition.version_token is not None and current.token != condition.version_token:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))

    async def delete_if_match(
        self,
        key: str,
        *,
        condition: WriteCondition | None = None,
    ) -> ConditionalWriteResult:
        current = await self.get_version(key)
        if condition:
            if condition.require_absent:
                if current.exists:
                    return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
                return ConditionalWriteResult(ok=True, current_version=current)
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        if current.exists:
            await self.delete(key)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))

    async def delete_batch_if_match(self, versions: Sequence[StorageVersion]) -> dict[str, ConditionalWriteResult]:
        """Delete each key only while it still holds its listed version; one result per key, keyed by storage key.

        ``ok`` means the key is gone. ``conflict`` means it was left in place because its version changed, because it
        was already gone (``current_version.exists`` tells which), or because an unresolved write holds it. A result is
        never raised: only a provider failure that leaves a key's fate unknown raises. The versions are the ones a
        listing (``StorageEntry.version()``) or ``get_version`` returned. This default checks and deletes key by key;
        backends with a native batch override it.
        """
        return {
            version.key: await self.delete_if_match(version.key, condition=WriteCondition(version_token=version.token))
            for version in versions
        }

    async def copy_batch_if_match(self, copies: Sequence[CopyRequest]) -> dict[str, ConditionalWriteResult]:
        """Copy each source to its destination while the source holds its listed version; results keyed by source key.

        ``ok`` means the destination now holds the bytes (an existing destination is replaced). ``conflict`` means the
        source changed or is gone, and nothing was written. Backends with a server-side copy never move a body through
        the engine. This default reads and writes each body, so only a backend that cannot copy natively inherits it.
        """
        results: dict[str, ConditionalWriteResult] = {}
        for copy in copies:
            current = await self.get_version(copy.source.key)
            if current.token != copy.source.token:
                results[copy.source.key] = ConditionalWriteResult(ok=False, conflict=True, current_version=current)
                continue
            await self.write_bytes(copy.destination_key, await self.read_bytes(copy.source.key))
            results[copy.source.key] = ConditionalWriteResult(
                ok=True, current_version=await self.get_version(copy.destination_key)
            )
        return results

    async def open_local_file(self, key: str) -> tuple[BinaryIO, os.stat_result] | None:
        """The key's regular file as an open read stream with the `fstat` of the same descriptor, or None.

        The caller owns the stream and closes it. Never copies and never reopens a path by name, so what the caller
        reads and the stat it holds describe one file. A backend that keeps nothing on a local disk returns None.
        """
        del key
        return None

    async def materialize(self, key: str) -> MaterializedFile | None:
        """A local file for ``key``, or None when the backend cannot provide one.

        The default cannot. Backends that copy the object override this and report the copy as owned; the caller
        closes the handle exactly once.
        """
        del key
        return None

    async def presign_download_url(
        self, key: str, filename: str | None = None, inline: bool = False, expires_in_seconds: int | None = None
    ) -> str | None:
        del key, filename, inline, expires_in_seconds
        return None


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
