"""Base storage types and interfaces."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path


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


@dataclass
class WriteCondition:
    version_token: str | None = None
    require_absent: bool = False


@dataclass
class ConditionalWriteResult:
    ok: bool
    conflict: bool = False
    current_version: StorageVersion | None = None


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

    async def read_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    async def read_bytes_bounded(self, key: str, max_bytes: int) -> bytes:
        """Read at most max_bytes, consuming only one additional byte to detect overflow.

        Raise StorageReadLimitExceededError on actual-byte overflow and FileNotFoundError
        for an absent key. Implementations must not first hash, promote, or fully
        read the object. A cancelled worker retains ownership of its resources
        until its bounded blocking operation finishes and closes them.
        """
        raise NotImplementedError

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        raw = await self.read_bytes(key)
        return raw.decode(encoding, errors=errors)

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

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        await self.write_bytes(key, content.encode(encoding), content_type="text/plain; charset=utf-8")

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def delete_tree(self, key: str) -> None:
        raise NotImplementedError

    async def stat(self, key: str) -> StorageEntry:
        raise NotImplementedError

    async def get_version(self, key: str) -> StorageVersion:
        try:
            entry = await self.stat(key)
        except FileNotFoundError:
            return StorageVersion(key=key, exists=False, is_dir=False)
        return StorageVersion(
            key=entry.key,
            exists=True,
            is_dir=entry.is_dir,
            size=entry.size,
            modified_at=entry.modified_at,
            etag=entry.etag,
            version_id=entry.version_id,
            content_hash=entry.content_hash,
        )

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

    async def local_path_for(self, key: str) -> Path | None:
        """The key's existing local file, or None. Never copies, so a caller can stream it without cleanup."""
        del key
        return None

    async def materialize(self, key: str) -> MaterializedFile | None:
        """A local file for ``key``, or None when the backend cannot provide one.

        The default reports an existing local file as not owned. Backends that copy the object override this and
        report the copy as owned; the caller closes the handle exactly once.
        """
        path = await self.local_path_for(key)
        return None if path is None else MaterializedFile(path)

    async def presign_download_url(self, key: str, filename: str | None = None, inline: bool = False) -> str | None:
        del key, filename, inline
        return None


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
