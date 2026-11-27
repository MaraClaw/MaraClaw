"""Per-path operations of the workspace mirror: each one settles a single path and reports what it changed."""

import hashlib
import os
import stat
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import partial
from io import FileIO
from pathlib import Path
from typing import BinaryIO, Final
from uuid import UUID

import anyio

from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageReadLimitExceededError,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.channel_files import (
    GuestFileChangedError,
    GuestStamp,
    delete_guest_file_if_unchanged,
    open_guest_file,
    stat_guest_file,
    write_guest_file,
)
from app.services.storage_runtime.quota_errors import StorageFileTooLargeError
from app.services.storage_runtime.upload_source import UploadSource
from app.services.workspace_mirror_local import proven_absent, unsettled
from app.services.workspace_mirror_plan import Action, Remote, conflict_path, local_matches, remote_matches
from app.services.workspace_mirror_scope import MAX_FILE_BYTES, LocalFile

_CONFLICT_ATTEMPTS = 20
# One buffer per transfer: every hash and copy below moves the bytes in chunks of this size.
_CHUNK_BYTES: Final = 1024 * 1024


class FrozenPathError(Exception):
    """The guest moved the path while the mirror worked on it; it stays as it is until the next pass."""


class Outcome(StrEnum):
    UNCHANGED = "unchanged"
    PUSHED = "pushed"
    PULLED = "pulled"
    DELETED_REMOTE = "deleted_remote"
    DELETED_LOCAL = "deleted_local"
    CONFLICT = "conflict"
    RECORDED = "recorded"
    TOO_LARGE = "too_large"
    DROPPED = "dropped"
    RACED = "raced"


@dataclass(frozen=True, slots=True)
class Done:
    """What one path's operation did; `entry` is the manifest row to upsert, `changed` the paths whose bytes moved."""

    outcome: Outcome
    entry: WorkspaceMirrorEntryRecord | None = None
    changed: tuple[str, ...] = ()
    size_bytes: int = 0
    conflict_path: str | None = None
    refusal: StorageFileTooLargeError | None = None


@dataclass(frozen=True, slots=True)
class PathWork:
    """One path of one agent pass: its base entry (current generation only) and what each side holds.

    `prior` is the path's manifest row of any generation: after a wiped guest disk it is the only record of the file.
    """

    agent_id: UUID
    agent_dir: Path
    store: StorageBackend
    generation: UUID
    rel: str
    entry: WorkspaceMirrorEntryRecord | None
    local: LocalFile | None
    remote: Remote | None
    prior: WorkspaceMirrorEntryRecord | None

    @property
    def key(self) -> str:
        return agent_storage_key(self.agent_id, self.rel)

    def record(
        self, local: GuestStamp | LocalFile, sha256: str | None, version: StorageVersion | None
    ) -> WorkspaceMirrorEntryRecord:
        return WorkspaceMirrorEntryRecord(
            agent_id=self.agent_id,
            path=self.rel,
            generation=self.generation,
            local_ino=local.ino,
            local_size=local.size,
            local_mtime_ns=local.mtime_ns,
            local_ctime_ns=local.ctime_ns,
            local_mode=stat.S_IMODE(local.mode),
            content_sha256=sha256,
            remote_etag=None if version is None else version.etag,
            remote_size=None if version is None else version.size,
            remote_token=None if version is None else version.token,
        )

    def stored_unchanged(self) -> bool:
        return self.entry is not None and self.remote is not None and remote_matches(self.entry, self.remote)


def _stamp(local: LocalFile) -> GuestStamp:
    return GuestStamp(local.ino, local.size, local.mtime_ns, local.ctime_ns, local.nlink, stat.S_IFREG)


async def _guest[T](call: Callable[[], T]) -> T:
    """Run a contained local operation on a worker thread; a path the guest moved, blocked or locked freezes alone."""
    try:
        return await anyio.to_thread.run_sync(call)
    except OSError as exc:
        if unsettled(exc):
            raise FrozenPathError(f"guest path moved or blocked: {exc}") from exc
        raise


def _sha256_of_prefix(stream: BinaryIO, size: int) -> str:
    """SHA-256 of the next `size` bytes of `stream`, one chunk at a time; a stream that ends early has changed."""
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(_CHUNK_BYTES, remaining))
        if not chunk:
            raise GuestFileChangedError(f"File ended {remaining} bytes before the {size} it held when it was opened")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def _open_hashed(agent_dir: Path, rel: str, walked: GuestStamp) -> tuple[UploadSource, GuestStamp, str]:
    """Open the guest file once through contained descriptors and hash it in chunks; the open file is the upload body.

    Raises `FrozenPathError` when the file is no longer the one the walk saw, grew past the cap or changed while it
    was hashed. The caller closes the returned source.
    """
    with open_guest_file(agent_dir, rel) as (fd, stamp):
        if not stamp.matches(walked):
            raise FrozenPathError("guest file changed since the walk")
        if stamp.size > MAX_FILE_BYTES:
            raise FrozenPathError("guest file grew past the mirror cap")
        stream = FileIO(os.dup(fd), "rb")
        try:
            sha256 = _sha256_of_prefix(stream, stamp.size)
            if not stamp.matches(GuestStamp.of(os.fstat(fd))):
                raise GuestFileChangedError(f"Guest file changed while it was being hashed: {rel}")
            _ = stream.seek(0)
        except BaseException:
            stream.close()
            raise
    return UploadSource(stream, stamp.size, ""), stamp, sha256


async def _unchanged_since(work: PathWork, stamp: GuestStamp) -> bool:
    """Whether the guest file still carries the stamp it had when its bytes were read."""
    try:
        return stamp.matches(await _guest(partial(stat_guest_file, work.agent_dir, work.rel)))
    except FrozenPathError:
        return False


@dataclass(frozen=True, slots=True)
class Fetched:
    """A stored object streamed into a temporary local file: where it is, what storage reported and its SHA-256."""

    path: Path
    version: StorageVersion
    sha256: str


def _measure(path: Path) -> tuple[int, str]:
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        return size, _sha256_of_prefix(stream, size)


@asynccontextmanager
async def _fetched(work: PathWork) -> AsyncGenerator[Fetched]:
    """Stream the stored object into a temp file through the store's own worker; the file is removed on exit.

    Freezes the path when the object vanished or exceeds the cap, or when its size is not the one storage reported
    for the listing's version, so a change under the download is never recorded as the version that was read.
    """
    try:
        version = await work.store.get_version_bounded(work.key, MAX_FILE_BYTES)
        if not version.exists:
            raise FileNotFoundError(work.key)
        copy = await work.store.materialize(work.key)
    except FileNotFoundError as exc:
        raise FrozenPathError("stored object vanished since the listing") from exc
    except StorageReadLimitExceededError as exc:
        raise FrozenPathError("stored object grew past the mirror cap") from exc
    if copy is None:
        raise RuntimeError(f"{type(work.store).__name__} cannot stream a stored object to a local file")
    try:
        size, sha256 = await anyio.to_thread.run_sync(_measure, copy.path)
        if size != version.size:
            raise FrozenPathError("stored object changed while it was being fetched")
        yield Fetched(copy.path, version, sha256)
    finally:
        copy.close()


def _write_from(
    path: Path, agent_dir: Path, rel: str, *, expected: GuestStamp | None, require_absent: bool, mode: int | None
) -> GuestStamp:
    with path.open("rb") as stream:
        return write_guest_file(agent_dir, rel, stream, expected=expected, require_absent=require_absent, mode=mode)


def _too_large(work: PathWork, local: GuestStamp | LocalFile, refusal: StorageFileTooLargeError | None) -> Done:
    entry = replace(work.record(local, None, None), skip_reason="too_large")
    if work.stored_unchanged() and work.entry is not None:
        entry = replace(
            entry,
            remote_etag=work.entry.remote_etag,
            remote_size=work.entry.remote_size,
            remote_token=work.entry.remote_token,
        )
    return Done(Outcome.TOO_LARGE, entry=entry, refusal=refusal)


async def _if_match(
    work: PathWork, token: str, attempt: Callable[[WriteCondition], Awaitable[ConditionalWriteResult]]
) -> ConditionalWriteResult:
    """A write or delete conditioned on the recorded version, retried once over a same-bytes rewrite.

    A versioned bucket gives a rewrite of the same bytes (a save without changes, a re-upload) a new version while
    ETag and size stay the recorded ones, so the listing shows no change and the recorded token never matches again.
    A stored version whose ETag or size differs is a real change and is never overwritten.
    """
    result = await attempt(WriteCondition(version_token=token))
    entry = work.entry
    if result.ok or entry is None or entry.remote_etag is None:
        return result
    current = result.current_version or await work.store.get_version(work.key)
    rewritten = current.exists and not current.is_dir and current.token != token
    if not rewritten or (current.etag, current.size) != (entry.remote_etag, entry.remote_size):
        return result
    return await attempt(WriteCondition(version_token=current.token))


async def push(work: PathWork, local: LocalFile, *, create: bool) -> Done:
    """Upload the guest file; a quota refusal other than "too large" propagates to the caller to classify.

    The bytes are hashed and then sent from one open descriptor, a chunk at a time. When the guest changed the file
    during the upload, the row records what storage now holds under the stamp that was read, with no content hash:
    the file then reads as changed and the next pass pushes it again instead of calling the two sides a conflict.
    """
    if local.size > MAX_FILE_BYTES:
        return _too_large(work, local, None)
    source, stamp, sha256 = await _guest(partial(_open_hashed, work.agent_dir, work.rel, _stamp(local)))
    try:
        entry = work.entry
        if (
            entry is not None
            and entry.skip_reason is None
            and entry.content_sha256 == sha256
            and work.stored_unchanged()
        ):
            return Done(
                Outcome.RECORDED,
                entry=replace(
                    entry,
                    generation=work.generation,
                    local_ino=stamp.ino,
                    local_size=stamp.size,
                    local_mtime_ns=stamp.mtime_ns,
                    local_ctime_ns=stamp.ctime_ns,
                    local_mode=stat.S_IMODE(stamp.mode),
                ),
                size_bytes=stamp.size,
            )
        token = None if entry is None or create else entry.remote_token
        try:
            if token is None:
                result = await work.store.write_source_if_match(
                    work.key, source, condition=WriteCondition(require_absent=True)
                )
            else:
                result = await _if_match(
                    work,
                    token,
                    lambda condition: work.store.write_source_if_match(work.key, source, condition=condition),
                )
        except StorageFileTooLargeError as exc:
            return _too_large(work, stamp, exc)
    finally:
        source.close()
    if not result.ok:
        return Done(Outcome.RACED)
    version = result.current_version or await work.store.get_version(work.key)
    if not await _unchanged_since(work, stamp):
        return Done(Outcome.RACED, entry=work.record(stamp, None, version), changed=(work.rel,))
    return Done(Outcome.PUSHED, entry=work.record(stamp, sha256, version), changed=(work.rel,), size_bytes=stamp.size)


def _restored_mode(prior: WorkspaceMirrorEntryRecord | None, version: StorageVersion) -> int | None:
    """The mode a pull gives a file it creates: the one recorded for these same stored bytes, else None for the default.

    Setuid and setgid never come back, and the owner can always read and write what the mirror restores.
    """
    etag = version.etag
    if prior is None or prior.local_mode is None or not etag or prior.remote_etag != etag:
        return None
    return (prior.local_mode & 0o777) | 0o600


async def pull(work: PathWork) -> Done:
    """Bring the stored bytes to the guest, unless they are the bytes the guest already holds.

    The object streams into a temp file and from there into a temp file beside its destination, which is renamed into
    place, so the guest sees the old file or the whole new one.
    """
    async with _fetched(work) as fetched:
        entry, local = work.entry, work.local
        if (
            local is not None
            and entry is not None
            and entry.skip_reason is None
            and local_matches(entry, local)
            and entry.content_sha256 == fetched.sha256
        ):
            return Done(
                Outcome.RECORDED,
                entry=work.record(local, fetched.sha256, fetched.version),
                size_bytes=local.size,
            )
        write = partial(
            _write_from,
            fetched.path,
            work.agent_dir,
            work.rel,
            expected=None if local is None else _stamp(local),
            require_absent=local is None,
            mode=_restored_mode(work.prior, fetched.version) if local is None else None,
        )
        stamp = await _guest(write)
        return Done(
            Outcome.PULLED,
            entry=work.record(stamp, fetched.sha256, fetched.version),
            changed=(work.rel,),
            size_bytes=fetched.version.size,
        )


def _save_conflict_copy(agent_dir: Path, rel: str, expected: GuestStamp, now: datetime) -> str:
    """Copy the guest file, unchanged since `expected`, to the first free conflict name, streaming from one descriptor."""
    with open_guest_file(agent_dir, rel) as (fd, stamp):
        if not stamp.matches(expected):
            raise GuestFileChangedError(f"Guest file changed before its conflict copy was made: {rel}")
        with os.fdopen(os.dup(fd), "rb") as source:
            for attempt in range(1, _CONFLICT_ATTEMPTS + 1):
                copy = conflict_path(rel, now, attempt)
                _ = source.seek(0)
                try:
                    _ = write_guest_file(agent_dir, copy, source, require_absent=True)
                except GuestFileChangedError:
                    continue
                return copy
    raise FrozenPathError(f"no free conflict path for {rel}")


async def compare(work: PathWork, local: LocalFile, now: datetime) -> Done:
    """Both sides hold bytes nobody reconciled: equal bytes are recorded, different ones go to storage's version."""
    if local.size > MAX_FILE_BYTES:
        raise FrozenPathError("guest file too large to compare")
    source, stamp, sha256 = await _guest(partial(_open_hashed, work.agent_dir, work.rel, _stamp(local)))
    source.close()
    async with _fetched(work) as fetched:
        if fetched.sha256 == sha256:
            return Done(Outcome.RECORDED, entry=work.record(stamp, sha256, fetched.version), size_bytes=stamp.size)
        copy = await _guest(partial(_save_conflict_copy, work.agent_dir, work.rel, stamp, now))
        write = partial(
            _write_from, fetched.path, work.agent_dir, work.rel, expected=stamp, require_absent=False, mode=None
        )
        written = await _guest(write)
        return Done(
            Outcome.CONFLICT,
            entry=work.record(written, fetched.sha256, fetched.version),
            changed=(work.rel, copy),
            size_bytes=fetched.version.size,
            conflict_path=copy,
        )


async def delete_remote(work: PathWork, entry: WorkspaceMirrorEntryRecord) -> Done:
    """Propagate a guest delete, only while storage still holds the version the entry recorded."""
    if entry.remote_token is None:
        raise FrozenPathError("no recorded stored version to condition the delete on")
    if not await _guest(partial(proven_absent, work.agent_dir, work.rel)):
        raise FrozenPathError("guest path reappeared")
    result = await _if_match(
        work, entry.remote_token, lambda condition: work.store.delete_if_match(work.key, condition=condition)
    )
    if not result.ok:
        return Done(Outcome.RACED)
    return Done(Outcome.DELETED_REMOTE, changed=(work.rel,))


async def delete_local(work: PathWork, local: LocalFile) -> Done:
    """Propagate a storage delete, only while the guest file is still the one the entry recorded."""
    if not await _guest(partial(delete_guest_file_if_unchanged, work.agent_dir, work.rel, _stamp(local))):
        return Done(Outcome.RACED)
    return Done(Outcome.DELETED_LOCAL, changed=(work.rel,))


async def run(action: Action, work: PathWork, now: datetime) -> Done:
    """Carry out the action `decide` chose; the action already guarantees which sides are present."""
    match action, work.local, work.entry:
        case Action.NOOP, _, _:
            size = work.remote.size if work.remote is not None else 0
            return Done(Outcome.UNCHANGED, size_bytes=size)
        case Action.DROP, _, _:
            return Done(Outcome.DROPPED)
        case Action.PULL, _, _:
            return await pull(work)
        case Action.PUSH_CREATE, LocalFile() as local, _:
            return await push(work, local, create=True)
        case Action.PUSH_UPDATE, LocalFile() as local, _:
            return await push(work, local, create=False)
        case Action.COMPARE, LocalFile() as local, _:
            return await compare(work, local, now)
        case Action.DELETE_LOCAL, LocalFile() as local, _:
            return await delete_local(work, local)
        case Action.DELETE_REMOTE, _, WorkspaceMirrorEntryRecord() as entry:
            return await delete_remote(work, entry)
        case _:
            raise RuntimeError(f"Mirror action {action} does not fit the sides of {work.rel}")
