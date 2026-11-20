"""Offline parts of the workspace mirror: the per-path decision table, freezes, conflict names, marker and lock."""

import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override
from uuid import UUID

import pytest

from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import StorageBackend, StorageSnapshot, StorageVersion, content_hash_bytes
from app.services.workspace_mirror import MirrorBusyError, mirror_lock
from app.services.workspace_mirror_local import MARKER, complete_generation, read_or_create_generation
from app.services.workspace_mirror_ops import Done, Outcome, PathWork, run
from app.services.workspace_mirror_plan import (
    SETTLE_NS,
    Action,
    Remote,
    conflict_path,
    decide,
    freeze_reason,
    mass_delete,
    scope_directories,
)
from app.services.workspace_mirror_scope import MAX_FILE_BYTES, LocalFile

NOW = datetime(2026, 11, 19, 12, 30, 45, tzinfo=UTC)
NOW_NS = int(NOW.timestamp()) * 1_000_000_000
REL = "workspace/notes/plan.md"
LOCAL = LocalFile(
    REL, ino=7, size=3, mtime_ns=NOW_NS - 60_000_000_000, ctime_ns=NOW_NS - 60_000_000_000, nlink=1, mode=0o644
)
EDITED = replace(LOCAL, size=4, mtime_ns=LOCAL.mtime_ns + 1)
REMOTE = Remote("etag-1", 3, "")
REMOTE_EDITED = Remote("etag-2", 4, "")


BASE = WorkspaceMirrorEntryRecord(
    agent_id=UUID(int=1),
    path=REL,
    generation=UUID(int=2),
    local_ino=LOCAL.ino,
    local_size=LOCAL.size,
    local_mtime_ns=LOCAL.mtime_ns,
    local_ctime_ns=LOCAL.ctime_ns,
    content_sha256="a" * 64,
    remote_etag=REMOTE.etag,
    remote_size=REMOTE.size,
    remote_token="token-1",
    synced_at=NOW - timedelta(minutes=5),
)


@pytest.mark.parametrize(
    ("entry", "local", "remote", "expected"),
    [
        pytest.param(None, LOCAL, None, Action.PUSH_CREATE, id="no-base-local-only"),
        pytest.param(None, None, REMOTE, Action.PULL, id="no-base-remote-only"),
        pytest.param(None, LOCAL, REMOTE, Action.COMPARE, id="no-base-both"),
        pytest.param(None, None, None, Action.DROP, id="no-base-neither"),
        pytest.param(BASE, LOCAL, REMOTE, Action.NOOP, id="unchanged"),
        pytest.param(BASE, EDITED, REMOTE, Action.PUSH_UPDATE, id="local-edited"),
        pytest.param(BASE, None, REMOTE, Action.DELETE_REMOTE, id="local-deleted"),
        pytest.param(BASE, LOCAL, REMOTE_EDITED, Action.PULL, id="remote-edited"),
        pytest.param(BASE, LOCAL, None, Action.DELETE_LOCAL, id="remote-deleted"),
        pytest.param(BASE, EDITED, REMOTE_EDITED, Action.COMPARE, id="both-edited"),
        pytest.param(BASE, None, None, Action.DROP, id="both-deleted"),
        pytest.param(BASE, None, REMOTE_EDITED, Action.PULL, id="local-deleted-remote-edited"),
        pytest.param(BASE, EDITED, None, Action.PUSH_CREATE, id="local-edited-remote-deleted"),
    ],
)
def test_decision_table(
    entry: WorkspaceMirrorEntryRecord | None, local: LocalFile | None, remote: Remote | None, expected: Action
):
    assert decide(REL, entry, local, remote, now=NOW) is expected


def test_a_deleted_root_file_is_restored_rather_than_deleted_from_storage():
    assert decide("soul.md", replace(BASE, path="soul.md"), None, REMOTE, now=NOW) is Action.PULL


@pytest.mark.parametrize(
    ("synced_ago", "local", "retry", "expected"),
    [
        pytest.param(timedelta(minutes=5), LOCAL, False, Action.NOOP, id="recent-unchanged-waits"),
        pytest.param(timedelta(hours=2), LOCAL, False, Action.PUSH_CREATE, id="retried-after-an-hour"),
        pytest.param(timedelta(minutes=5), EDITED, False, Action.PUSH_CREATE, id="retried-once-changed"),
        pytest.param(timedelta(minutes=5), LOCAL, True, Action.PUSH_CREATE, id="share-retries-now"),
    ],
)
def test_a_too_large_file_is_offered_again_only_when_it_changed_or_an_hour_passed(
    synced_ago: timedelta, local: LocalFile, retry: bool, expected: Action
):
    entry = replace(
        BASE, skip_reason="too_large", remote_etag=None, remote_size=None, remote_token=None, synced_at=NOW - synced_ago
    )
    assert decide(REL, entry, local, None, now=NOW, retry_too_large=retry) is expected


def test_a_stored_edit_over_an_unsynced_too_large_file_goes_through_the_conflict_rule():
    entry = replace(BASE, skip_reason="too_large", content_sha256=None)
    assert decide(REL, entry, LOCAL, REMOTE_EDITED, now=NOW) is Action.COMPARE


@pytest.mark.parametrize(
    ("local", "remote", "settle_local", "frozen"),
    [
        pytest.param(replace(LOCAL, nlink=2), None, True, True, id="hard-link"),
        pytest.param(replace(LOCAL, mtime_ns=NOW_NS - SETTLE_NS + 1), None, True, True, id="local-settling"),
        pytest.param(replace(LOCAL, mtime_ns=NOW_NS - SETTLE_NS + 1), None, False, False, id="share-skips-settle"),
        pytest.param(None, Remote("e", MAX_FILE_BYTES + 1, ""), True, True, id="stored-too-large"),
        pytest.param(None, Remote("e", 1, str(NOW - timedelta(seconds=1))), True, True, id="stored-settling"),
        pytest.param(LOCAL, Remote("e", 1, str(NOW - timedelta(minutes=1))), True, False, id="both-settled"),
    ],
)
def test_freeze_reasons(local: LocalFile | None, remote: Remote | None, settle_local: bool, frozen: bool):
    reason = freeze_reason(local, remote, None, now_ns=NOW_NS, settle_local=settle_local)
    assert (reason is not None) is frozen


@pytest.mark.parametrize(
    ("remote", "frozen"),
    [
        pytest.param(replace(REMOTE, modified_at=str(NOW - timedelta(seconds=1))), False, id="recorded-just-written"),
        pytest.param(replace(REMOTE_EDITED, modified_at=str(NOW - timedelta(seconds=1))), True, id="changed-settling"),
    ],
)
def test_a_stored_object_settles_only_when_it_differs_from_the_record(remote: Remote, frozen: bool):
    reason = freeze_reason(LOCAL, remote, BASE, now_ns=NOW_NS, settle_local=False)
    assert (reason is not None) is frozen


@pytest.mark.parametrize(
    ("delete", "other"), [(Action.DELETE_REMOTE, Action.DELETE_LOCAL), (Action.DELETE_LOCAL, Action.DELETE_REMOTE)]
)
@pytest.mark.parametrize(
    ("deletes", "base_count", "guarded"),
    [(19, 19, False), (20, 40, True), (20, 41, False)],
)
def test_mass_delete_needs_twenty_deletes_of_one_side_and_half_the_base(
    deletes: int, base_count: int, guarded: bool, delete: Action, other: Action
):
    actions = {f"workspace/{index}.md": delete for index in range(deletes)}
    actions["workspace/other.md"] = Action.PUSH_UPDATE
    assert (mass_delete(actions, base_count, delete), mass_delete(actions, base_count, other)) == (guarded, False)


@pytest.mark.parametrize(
    ("rel", "attempt", "expected"),
    [
        (REL, 1, "workspace/conflicts/workspace/notes/plan (conflict 2026-11-19 123045).md"),
        ("soul.md", 1, "workspace/conflicts/soul (conflict 2026-11-19 123045).md"),
        ("workspace/Makefile", 2, "workspace/conflicts/workspace/Makefile (conflict 2026-11-19 123045 2)"),
    ],
)
def test_conflict_copies_live_under_workspace_conflicts_stamped_before_the_suffix(
    rel: str, attempt: int, expected: str
):
    assert conflict_path(rel, NOW, attempt) == expected


def test_every_directory_a_scope_prefix_runs_through_is_checked():
    assert set(scope_directories()) == {"workspace", "memory", "daily_reports", "skills", "media", "media/outbound"}


def test_a_generation_is_a_base_only_once_a_pass_finished_and_an_invalid_marker_is_replaced(tmp_path: Path):
    generation, had_base = read_or_create_generation(tmp_path)
    pending = read_or_create_generation(tmp_path)
    complete_generation(tmp_path, generation)
    assert (had_base, pending, read_or_create_generation(tmp_path)) == (False, (generation, False), (generation, True))
    (tmp_path / MARKER).unlink()
    (tmp_path / MARKER).symlink_to(tmp_path / "elsewhere")
    replaced, had_base = read_or_create_generation(tmp_path)
    assert (replaced != generation, had_base, (tmp_path / MARKER).is_symlink()) == (True, False, False)
    _ = (tmp_path / MARKER).write_bytes(b"not a uuid")
    assert read_or_create_generation(tmp_path)[1] is False


def test_a_directory_on_the_marker_path_is_moved_aside_and_a_new_generation_starts(tmp_path: Path):
    (tmp_path / MARKER).mkdir()
    _ = (tmp_path / MARKER / "notes.txt").write_bytes(b"guest")

    generation, had_base = read_or_create_generation(tmp_path)

    aside = [path for path in tmp_path.iterdir() if path.name.startswith(f"{MARKER}.displaced-")]
    assert (had_base, read_or_create_generation(tmp_path), [(path / "notes.txt").read_bytes() for path in aside]) == (
        False,
        (generation, False),
        [b"guest"],
    )


class _VersionedStore(StorageBackend):
    """A versioned bucket: every write gets a new version id, while the same bytes keep the same ETag."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.writes: int = 0

    def held(self, key: str) -> bytes | None:
        found = self.objects.get(key)
        return None if found is None else found[0]

    @override
    async def get_version(self, key: str) -> StorageVersion:
        found = self.objects.get(key)
        if found is None:
            return StorageVersion(key, exists=False, is_dir=False)
        data, version_id = found
        etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
        return StorageVersion(key, exists=True, is_dir=False, size=len(data), etag=etag, version_id=version_id)

    @override
    async def read_snapshot_bounded(self, key: str, max_bytes: int) -> StorageSnapshot:
        data = self.objects[key][0]
        return StorageSnapshot(data, await self.get_version(key), content_hash_bytes(data))

    @override
    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.writes += 1
        self.objects[key] = (data, f"v{self.writes}")

    @override
    async def delete(self, key: str) -> None:
        del self.objects[key]


STORED_ETAG = hashlib.md5(b"stored", usedforsecurity=False).hexdigest()


@pytest.fixture
def umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        _ = os.umask(previous)


def _guest_file(root: Path, data: bytes, mode: int) -> Path:
    path = root / REL
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(data)
    path.chmod(mode)
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _local_file(path: Path) -> LocalFile:
    info = path.stat()
    return LocalFile(
        REL, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink, stat.S_IMODE(info.st_mode)
    )


async def _store_holding(data: bytes) -> tuple[_VersionedStore, StorageVersion]:
    store = _VersionedStore()
    key = agent_storage_key(BASE.agent_id, REL)
    await store.write_bytes(key, data)
    return store, await store.get_version(key)


@pytest.mark.parametrize(
    ("rewrite", "action", "outcome", "stored"),
    [
        pytest.param(b"abc", Action.PUSH_UPDATE, Outcome.PUSHED, b"guest edit", id="push-over-same-bytes"),
        pytest.param(b"abc", Action.DELETE_REMOTE, Outcome.DELETED_REMOTE, None, id="delete-over-same-bytes"),
        pytest.param(b"xyz", Action.PUSH_UPDATE, Outcome.RACED, b"xyz", id="push-keeps-a-real-change"),
        pytest.param(b"xyz", Action.DELETE_REMOTE, Outcome.RACED, b"xyz", id="delete-keeps-a-real-change"),
    ],
)
async def test_a_same_bytes_rewrite_under_a_new_version_is_retried_once_but_a_real_change_is_kept(
    tmp_path: Path, rewrite: bytes, action: Action, outcome: Outcome, stored: bytes | None
):
    store = _VersionedStore()
    key = agent_storage_key(BASE.agent_id, REL)
    await store.write_bytes(key, b"abc")
    recorded = await store.get_version(key)
    await store.write_bytes(key, rewrite)
    entry = replace(BASE, remote_etag=recorded.etag, remote_size=recorded.size, remote_token=recorded.token)
    local: LocalFile | None = None
    if action is Action.PUSH_UPDATE:
        local = _local_file(_guest_file(tmp_path, b"guest edit", 0o644))
    current = await store.get_version(key)
    remote = Remote(current.etag, current.size, "")
    work = PathWork(BASE.agent_id, tmp_path, store, BASE.generation, REL, entry, local, remote, entry)

    done = await run(action, work, NOW)

    assert (done.outcome, store.held(key)) == (outcome, stored)


async def _pull_into(root: Path, prior: WorkspaceMirrorEntryRecord | None) -> Done:
    """Pull the stored bytes into a directory that lacks the file, as a restore after a wiped guest disk does."""
    store, stored = await _store_holding(b"stored")
    remote = Remote(stored.etag, stored.size, "")
    work = PathWork(BASE.agent_id, root, store, UUID(int=9), REL, None, None, remote, prior)
    return await run(Action.PULL, work, NOW)


@pytest.mark.usefixtures("umask_022")
@pytest.mark.parametrize(
    ("recorded", "etag", "expected"),
    [
        pytest.param(0o755, STORED_ETAG, 0o755, id="exec-bit-comes-back"),
        pytest.param(0o4700, STORED_ETAG, 0o700, id="setuid-is-dropped"),
        pytest.param(0o2755, STORED_ETAG, 0o755, id="setgid-is-dropped"),
        pytest.param(0o400, STORED_ETAG, 0o600, id="the-owner-can-always-read-and-write"),
        pytest.param(0o755, "etag-of-other-bytes", 0o644, id="other-stored-bytes-get-the-default"),
        pytest.param(None, STORED_ETAG, 0o644, id="no-recorded-mode-gets-the-default"),
    ],
)
async def test_a_pull_that_creates_a_file_restores_the_mode_recorded_for_the_same_stored_bytes(
    tmp_path: Path, recorded: int | None, etag: str, expected: int
):
    prior = replace(BASE, remote_etag=etag, local_mode=recorded)

    done = await _pull_into(tmp_path, prior)

    assert done.entry is not None
    assert (done.outcome, _mode(tmp_path / REL), done.entry.local_mode) == (Outcome.PULLED, expected, expected)


@pytest.mark.usefixtures("umask_022")
async def test_a_pull_that_creates_a_file_with_no_manifest_row_uses_the_default_mode(tmp_path: Path):
    done = await _pull_into(tmp_path, None)

    assert done.entry is not None
    assert (done.outcome, _mode(tmp_path / REL), done.entry.local_mode) == (Outcome.PULLED, 0o644, 0o644)


@pytest.mark.usefixtures("umask_022")
async def test_a_pull_over_an_existing_file_keeps_that_files_mode(tmp_path: Path):
    store, stored = await _store_holding(b"stored")
    path = _guest_file(tmp_path, b"guest", 0o640)
    prior = replace(BASE, remote_etag=STORED_ETAG, local_mode=0o755)
    work = PathWork(
        BASE.agent_id,
        tmp_path,
        store,
        UUID(int=9),
        REL,
        None,
        _local_file(path),
        Remote(stored.etag, stored.size, ""),
        prior,
    )

    done = await run(Action.PULL, work, NOW)

    assert done.entry is not None
    assert (done.outcome, _mode(path), done.entry.local_mode) == (Outcome.PULLED, 0o640, 0o640)


@pytest.mark.usefixtures("umask_022")
async def test_a_conflict_copy_gets_the_default_mode_and_the_replaced_file_keeps_its_own(tmp_path: Path):
    store, stored = await _store_holding(b"stored")
    path = _guest_file(tmp_path, b"guest", 0o755)
    work = PathWork(
        BASE.agent_id,
        tmp_path,
        store,
        BASE.generation,
        REL,
        None,
        _local_file(path),
        Remote(stored.etag, stored.size, ""),
        None,
    )

    done = await run(Action.COMPARE, work, NOW)

    assert done.entry is not None
    assert done.conflict_path is not None
    copy = tmp_path / done.conflict_path
    assert (done.outcome, copy.read_bytes(), _mode(copy)) == (Outcome.CONFLICT, b"guest", 0o644)
    assert (path.read_bytes(), _mode(path), done.entry.local_mode) == (b"stored", 0o755, 0o755)


@pytest.mark.usefixtures("umask_022")
async def test_a_push_records_the_mode_of_the_guest_file(tmp_path: Path):
    store, stored = await _store_holding(b"abc")
    path = _guest_file(tmp_path, b"guest edit", 0o755)
    entry = replace(BASE, remote_etag=stored.etag, remote_size=stored.size, remote_token=stored.token, local_mode=0o644)
    work = PathWork(
        BASE.agent_id,
        tmp_path,
        store,
        BASE.generation,
        REL,
        entry,
        _local_file(path),
        Remote(stored.etag, stored.size, ""),
        entry,
    )

    done = await run(Action.PUSH_UPDATE, work, NOW)

    assert done.entry is not None
    assert (done.outcome, done.entry.local_mode) == (Outcome.PUSHED, 0o755)


@pytest.mark.usefixtures("umask_022")
async def test_a_chmod_alone_is_recorded_with_its_new_mode_and_uploads_nothing(tmp_path: Path):
    store, stored = await _store_holding(b"abc")
    path = _guest_file(tmp_path, b"abc", 0o644)
    before = _local_file(path)
    entry = replace(
        BASE,
        local_ino=before.ino,
        local_size=before.size,
        local_mtime_ns=before.mtime_ns,
        local_ctime_ns=before.ctime_ns,
        local_mode=0o644,
        content_sha256=content_hash_bytes(b"abc"),
        remote_etag=stored.etag,
        remote_size=stored.size,
        remote_token=stored.token,
    )
    path.chmod(0o755)
    work = PathWork(
        BASE.agent_id,
        tmp_path,
        store,
        BASE.generation,
        REL,
        entry,
        _local_file(path),
        Remote(stored.etag, stored.size, ""),
        entry,
    )

    done = await run(Action.PUSH_UPDATE, work, NOW)

    assert done.entry is not None
    assert (done.outcome, done.entry.local_mode, store.writes) == (Outcome.RECORDED, 0o755, 1)


async def test_a_held_mirror_lock_makes_another_pass_busy(tmp_path: Path):
    agent_dir = tmp_path / "agent"
    async with mirror_lock(agent_dir, wait_seconds=0):
        with pytest.raises(MirrorBusyError):
            async with mirror_lock(agent_dir, wait_seconds=0):
                pass
    async with mirror_lock(agent_dir, wait_seconds=0):
        pass
