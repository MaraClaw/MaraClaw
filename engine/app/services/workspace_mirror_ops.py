"""Per-path operations of the workspace mirror: each one settles a single path and reports what it changed."""

import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from uuid import UUID

import anyio

from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageReadLimitExceededError,
    StorageSnapshot,
    StorageVersion,
    WriteCondition,
    content_hash_bytes,
)
from app.services.storage_runtime.channel_files import (
    GuestFileChangedError,
    GuestStamp,
    delete_guest_file_if_unchanged,
    read_guest_file_stamped,
    write_guest_file,
)
from app.services.storage_runtime.quota_errors import StorageFileTooLargeError
from app.services.workspace_mirror_local import proven_absent, unsettled
from app.services.workspace_mirror_plan import Action, Remote, conflict_path, local_matches, remote_matches
from app.services.workspace_mirror_scope import MAX_FILE_BYTES, LocalFile

_CONFLICT_ATTEMPTS = 20


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
    """One path of one agent pass: its base entry (current generation only) and what each side holds."""

    agent_id: UUID
    agent_dir: Path
    store: StorageBackend
    generation: UUID
    rel: str
    entry: WorkspaceMirrorEntryRecord | None
    local: LocalFile | None
    remote: Remote | None

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


async def _read_local(work: PathWork, local: LocalFile) -> tuple[bytes, GuestStamp, str]:
    try:
        data, stamp = await _guest(partial(read_guest_file_stamped, work.agent_dir, work.rel, MAX_FILE_BYTES))
    except StorageReadLimitExceededError as exc:
        raise FrozenPathError("guest file grew past the mirror cap") from exc
    if not stamp.matches(_stamp(local)):
        raise FrozenPathError("guest file changed since the walk")
    return data, stamp, await anyio.to_thread.run_sync(content_hash_bytes, data)


async def _snapshot(work: PathWork) -> StorageSnapshot:
    try:
        return await work.store.read_snapshot_bounded(work.key, MAX_FILE_BYTES)
    except FileNotFoundError as exc:
        raise FrozenPathError("stored object vanished since the listing") from exc
    except StorageReadLimitExceededError as exc:
        raise FrozenPathError("stored object grew past the mirror cap") from exc


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
    """Upload the guest file; a quota refusal other than "too large" propagates to the caller to classify."""
    if local.size > MAX_FILE_BYTES:
        return _too_large(work, local, None)
    data, stamp, sha256 = await _read_local(work, local)
    entry = work.entry
    if entry is not None and entry.skip_reason is None and entry.content_sha256 == sha256 and work.stored_unchanged():
        return Done(
            Outcome.RECORDED,
            entry=replace(
                entry,
                generation=work.generation,
                local_ino=stamp.ino,
                local_size=stamp.size,
                local_mtime_ns=stamp.mtime_ns,
                local_ctime_ns=stamp.ctime_ns,
            ),
            size_bytes=stamp.size,
        )
    token = None if entry is None or create else entry.remote_token
    try:
        if token is None:
            result = await work.store.create_bytes_if_absent(work.key, data)
        else:
            result = await _if_match(
                work, token, lambda condition: work.store.write_bytes_if_match(work.key, data, condition=condition)
            )
    except StorageFileTooLargeError as exc:
        return _too_large(work, stamp, exc)
    if not result.ok:
        return Done(Outcome.RACED)
    version = result.current_version or await work.store.get_version(work.key)
    return Done(Outcome.PUSHED, entry=work.record(stamp, sha256, version), changed=(work.rel,), size_bytes=len(data))


async def pull(work: PathWork) -> Done:
    """Bring the stored bytes to the guest, unless they are the bytes the guest already holds."""
    snapshot = await _snapshot(work)
    entry, local = work.entry, work.local
    if (
        local is not None
        and entry is not None
        and entry.skip_reason is None
        and local_matches(entry, local)
        and entry.content_sha256 == snapshot.sha256
    ):
        return Done(
            Outcome.RECORDED, entry=work.record(local, snapshot.sha256, snapshot.version), size_bytes=local.size
        )
    write = partial(
        write_guest_file,
        work.agent_dir,
        work.rel,
        snapshot.data,
        expected=None if local is None else _stamp(local),
        require_absent=local is None,
    )
    stamp = await _guest(write)
    entry = work.record(stamp, snapshot.sha256, snapshot.version)
    return Done(Outcome.PULLED, entry=entry, changed=(work.rel,), size_bytes=len(snapshot.data))


def _save_conflict_copy(agent_dir: Path, rel: str, data: bytes, now: datetime) -> str:
    for attempt in range(1, _CONFLICT_ATTEMPTS + 1):
        copy = conflict_path(rel, now, attempt)
        try:
            _ = write_guest_file(agent_dir, copy, data, require_absent=True)
        except GuestFileChangedError:
            continue
        return copy
    raise FrozenPathError(f"no free conflict path for {rel}")


async def compare(work: PathWork, local: LocalFile, now: datetime) -> Done:
    """Both sides hold bytes nobody reconciled: equal bytes are recorded, different ones go to storage's version."""
    if local.size > MAX_FILE_BYTES:
        raise FrozenPathError("guest file too large to compare")
    data, stamp, sha256 = await _read_local(work, local)
    snapshot = await _snapshot(work)
    if snapshot.sha256 == sha256:
        return Done(Outcome.RECORDED, entry=work.record(stamp, sha256, snapshot.version), size_bytes=len(data))
    copy = await _guest(partial(_save_conflict_copy, work.agent_dir, work.rel, data, now))
    written = await _guest(partial(write_guest_file, work.agent_dir, work.rel, snapshot.data, expected=stamp))
    return Done(
        Outcome.CONFLICT,
        entry=work.record(written, snapshot.sha256, snapshot.version),
        changed=(work.rel, copy),
        size_bytes=len(snapshot.data),
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
