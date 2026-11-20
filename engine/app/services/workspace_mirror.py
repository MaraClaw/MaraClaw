"""Two-way mirror between each guest's bind-mounted state directory and the durable agent storage (S3 mode only).

Both sides are compared against a per-path manifest; a path changed on one side is copied to the other, a path
changed on both goes to storage's version and the guest's bytes are kept as a conflict copy. Anything mid-change
or unprovable freezes for a pass instead of being read as a delete. Lock order is the shared setup lock (via
`guest_write_section`) then the mirror lock; never the other way round.
"""

import fcntl
import stat
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import anyio
from fastapi import HTTPException

from app.config import get_settings
from app.core.logging import logger
from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.dao.workspace_mirror_entry_dao import workspace_mirror_entry_dao
from app.records.agent import AgentRecord
from app.services.agent_context_cache import invalidate_for_workspace_paths
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, wait_for_flock
from app.services.openclaw_update_exclusion import guest_write_section
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from app.services.storage import get_storage_backend
from app.services.storage_runtime.agent_files import agent_storage_key
from app.services.storage_runtime.base import StorageBackend
from app.services.storage_runtime.channel_files import lstat_guest_path
from app.services.storage_runtime.fallback import FallbackStorageBackend
from app.services.storage_runtime.local import LocalStorageBackend
from app.services.storage_runtime.quota_errors import (
    StorageFileTooLargeError,
    StorageKeyPendingError,
    StorageQuotaExceededError,
)
from app.services.workspace_mirror_local import complete_generation, read_or_create_generation, unsettled
from app.services.workspace_mirror_ops import Done, FrozenPathError, Outcome, PathWork, run
from app.services.workspace_mirror_plan import (
    MASS_DELETE_RESTORES,
    PUSHES,
    STORE_WRITES,
    Action,
    Remote,
    decide,
    freeze_reason,
    mass_delete,
)
from app.services.workspace_mirror_scope import MAX_FILE_BYTES, LocalFile, in_scope, is_shareable
from app.services.workspace_mirror_sides import (
    Sides,
    frozen_paths,
    gather,
    moment,
    plan,
    recorded_count,
    stores_nothing,
)

PASS_BUDGET_SECONDS = 20.0
SYNC_LOCK_WAIT_SECONDS = 10.0
_TRANSFER_PENDING = 423

# Clocks, module-level so tests can move time instead of sleeping.
_wall_clock_ns = time.time_ns
_monotonic = time.monotonic


class MirrorMode(StrEnum):
    FULL = "full"
    PULL_ONLY = "pull_only"


class MirrorBusyError(Exception):
    """Another mirror pass holds the agent's mirror lock; the share route answers 503 file_sync_busy."""


class MirrorConflictError(Exception):
    """Storage held a newer version; it replaced the guest file and the guest bytes were saved at conflict_path."""

    def __init__(self, path: str, conflict_path: str):
        super().__init__(f"{path} conflicted with a newer stored version")
        self.path: str = path
        self.conflict_path: str = conflict_path


class MirrorReconcileError(Exception):
    """An agent pass stopped early; had_base says whether an earlier pass of the directory's generation finished."""

    def __init__(self, message: str, *, had_base: bool):
        super().__init__(message)
        self.had_base: bool = had_base


@dataclass(frozen=True, slots=True)
class SyncedFile:
    path: str
    key: str
    size_bytes: int


@dataclass(slots=True)
class ReconcileReport:
    had_base: bool = False
    pushed: int = 0
    pulled: int = 0
    deleted_remote: int = 0
    deleted_local: int = 0
    conflicts: int = 0
    frozen: int = 0
    too_large: int = 0
    quota_exhausted: bool = False
    mass_delete_guarded: bool = False
    remote_missing_guarded: bool = False
    budget_exhausted: bool = False

    @property
    def changed(self) -> bool:
        moved = self.pushed + self.pulled + self.deleted_remote + self.deleted_local + self.conflicts + self.too_large
        return moved > 0 or self.quota_exhausted or self.mass_delete_guarded or self.remote_missing_guarded

    @property
    def finished(self) -> bool:
        """Every path was settled or deliberately frozen: no budget cut and no hold on the whole pass."""
        return not (self.budget_exhausted or self.remote_missing_guarded)


def agent_state_dir(agent_id: UUID) -> Path:
    """The engine-side directory bind-mounted at /home/node/custom-state in the agent's guest container."""
    settings = get_settings()
    return Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR) / str(agent_id)


def durable_store() -> StorageBackend | None:
    """The store guest files are mirrored into, or None when storage already is the guest directory (local mode)."""
    if not get_settings().WORKSPACE_MIRROR_ENABLED:
        return None
    match get_storage_backend():
        case LocalStorageBackend():
            return None
        case FallbackStorageBackend() as storage:
            return storage.primary
        case storage:
            return storage


_COUNTERS = {
    Outcome.PUSHED: "pushed",
    Outcome.PULLED: "pulled",
    Outcome.DELETED_REMOTE: "deleted_remote",
    Outcome.DELETED_LOCAL: "deleted_local",
    Outcome.CONFLICT: "conflicts",
    Outcome.TOO_LARGE: "too_large",
}
_FORGETS = frozenset({Outcome.DELETED_REMOTE, Outcome.DELETED_LOCAL, Outcome.DROPPED})


@dataclass(slots=True)
class _Ledger:
    """Writes each finished path to the manifest as it lands and tallies the pass."""

    agent_id: UUID
    report: ReconcileReport
    upserted: set[str] = field(default_factory=set)
    forgotten: set[str] = field(default_factory=set)
    changed: list[str] = field(default_factory=list)

    async def apply(self, rel: str, done: Done) -> None:
        if done.entry is not None:
            await workspace_mirror_entry_dao.upsert(done.entry)
            self.upserted.add(rel)
        if done.outcome in _FORGETS:
            self.forgotten.add(rel)
        counter = _COUNTERS.get(done.outcome)
        if counter is not None:
            setattr(self.report, counter, getattr(self.report, counter) + 1)
        self.changed.extend(done.changed)

    async def forget(self, stale: set[str]) -> None:
        """Drop the rows of paths gone from both sides and of every other generation this pass did not rewrite."""
        await workspace_mirror_entry_dao.delete_paths(self.agent_id, sorted(self.forgotten | (stale - self.upserted)))

    async def invalidate(self) -> None:
        """Prompt caches of every path whose bytes moved, even when the pass stopped part way."""
        if self.changed:
            await invalidate_for_workspace_paths(self.agent_id, *self.changed)


async def _reconcile(
    agent_id: UUID,
    agent_dir: Path,
    store: StorageBackend,
    mode: MirrorMode,
    budget_seconds: float,
    report: ReconcileReport,
) -> None:
    deadline = _monotonic() + budget_seconds
    generation, report.had_base = await anyio.to_thread.run_sync(read_or_create_generation, agent_dir)
    sides = await gather(agent_id, agent_dir, store, generation, budget_seconds)
    recorded = recorded_count(sides.manifest, generation)
    if recorded > 0 and not sides.remote:
        report.remote_missing_guarded = True
        logger.warning(
            f"Workspace mirror of agent {agent_id} listed nothing in storage although {recorded} mirrored files are "
            "recorded there; it holds the whole pass rather than read a wrong or wiped bucket as deletes"
        )
        return
    paths = sorted(rel for rel in sides.local.keys() | sides.remote.keys() | sides.manifest.keys() if in_scope(rel))
    stale = {rel for rel, entry in sides.manifest.items() if entry.generation != generation or not in_scope(rel)}
    now_ns = _wall_clock_ns()
    frozen = await frozen_paths(agent_dir, sides, paths)
    actions = plan(sides, paths, frozen, now_ns)
    report.frozen = len(frozen)
    for rel, reason in frozen.items():
        logger.debug(f"Workspace mirror froze {rel} of agent {agent_id}: {reason}")
    base_count = sum(1 for entry in sides.manifest.values() if entry.generation == generation)
    for delete, restore in MASS_DELETE_RESTORES.items():
        if not mass_delete(actions, base_count, delete):
            continue
        report.mass_delete_guarded = True
        held = [rel for rel, action in actions.items() if action is delete]
        logger.warning(
            f"Workspace mirror of agent {agent_id} held back {len(held)} {delete} actions of {base_count} mirrored "
            f"files and turns them into {restore}"
        )
        # Without a base a path is only ever copied across, so the hold outlasts a restore that spans passes.
        _ = await workspace_mirror_entry_dao.delete_paths(agent_id, held)
        for rel in held:
            del sides.manifest[rel]
        actions |= dict.fromkeys(held, restore)
    if mode is MirrorMode.PULL_ONLY:
        actions = {rel: action for rel, action in actions.items() if action not in STORE_WRITES}
    ledger = _Ledger(agent_id, report)
    try:
        await _execute(agent_id, agent_dir, store, sides, actions, deadline, ledger)
        await ledger.forget(stale)
    finally:
        await ledger.invalidate()
    if report.finished and not report.had_base:
        await anyio.to_thread.run_sync(complete_generation, agent_dir, generation)


async def _execute(
    agent_id: UUID,
    agent_dir: Path,
    store: StorageBackend,
    sides: Sides,
    actions: dict[str, Action],
    deadline: float,
    ledger: _Ledger,
) -> None:
    report = ledger.report
    now = moment(_wall_clock_ns())
    for rel, action in actions.items():
        if _monotonic() >= deadline:
            report.budget_exhausted = True
            return
        if action in PUSHES and report.quota_exhausted:
            continue
        work = PathWork(
            agent_id,
            agent_dir,
            store,
            sides.generation,
            rel,
            sides.base(rel),
            sides.local.get(rel),
            sides.remote.get(rel),
        )
        try:
            done = await run(action, work, now)
        except (FrozenPathError, StorageKeyPendingError) as exc:
            report.frozen += 1
            logger.info(f"Workspace mirror froze {rel} of agent {agent_id}: {type(exc).__name__}: {exc}")
            continue
        except StorageQuotaExceededError:
            report.quota_exhausted = True
            logger.warning(f"Workspace mirror of agent {agent_id} reached the storage limit; uploads wait for room")
            continue
        await ledger.apply(rel, done)


async def reconcile_agent(
    agent_id: UUID,
    agent_dir: Path,
    store: StorageBackend,
    *,
    mode: MirrorMode,
    budget_seconds: float = PASS_BUDGET_SECONDS,
) -> ReconcileReport:
    """One pass over every in-scope path of one agent. Takes no locks: the caller holds them.

    A PULL_ONLY pass restoring a directory whose generation never finished a pass raises unless it finishes now,
    so a start never boots a partly restored guest; the files it did pull keep their rows for the next try.
    """
    report = ReconcileReport()
    try:
        await _reconcile(agent_id, agent_dir, store, mode, budget_seconds, report)
    except Exception as exc:
        raise MirrorReconcileError(
            f"Workspace mirror pass stopped: {type(exc).__name__}: {exc}", had_base=report.had_base
        ) from exc
    if mode is MirrorMode.PULL_ONLY and not report.had_base and not report.finished:
        raise MirrorReconcileError(f"Workspace mirror restore did not finish: {report}", had_base=False)
    return report


@asynccontextmanager
async def mirror_lock(agent_dir: Path, *, wait_seconds: float) -> AsyncGenerator[None]:
    """Exclusive per-agent mirror flock beside the setup lock; `MirrorBusyError` when it stays taken."""
    lock_dir = agent_dir.parent / "_setup_locks"
    await anyio.Path(lock_dir).mkdir(parents=True, exist_ok=True)
    lock = (lock_dir / f"{agent_dir.name}.mirror.lock").open("a+b")
    try:
        try:
            await wait_for_flock(lock, fcntl.LOCK_EX, wait_seconds)
        except WorkspaceLockTimeoutError as exc:
            raise MirrorBusyError("Another workspace mirror pass is running for this agent") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        lock.close()


async def mirror_agent(agent: AgentRecord) -> ReconcileReport | None:
    """Worker entry: one FULL pass, or None when the agent is skipped this round."""
    store = durable_store()
    if store is None:
        return None
    agent_dir = agent_state_dir(agent.id)
    if not await anyio.Path(agent_dir).is_dir():
        return None
    try:
        async with guest_write_section(agent.id), mirror_lock(agent_dir, wait_seconds=0):
            memo_drop("agent", agent.id)
            if await agent_dao.get(agent.id) is None:
                return None
            report = await reconcile_agent(agent.id, agent_dir, store, mode=MirrorMode.FULL)
    except (OpenClawUpdateInProgressError, WorkspaceLockTimeoutError, MirrorBusyError) as exc:
        logger.debug(f"Workspace mirror skipped agent {agent.id}: {type(exc).__name__}")
        return None
    except HTTPException as exc:
        if exc.status_code != _TRANSFER_PENDING:
            raise
        logger.debug(f"Workspace mirror skipped agent {agent.id}: transfer cleanup pending")
        return None
    if report.changed:
        logger.info(f"Workspace mirror agent {agent.id}: {report}")
    return report


async def _local_file(agent_dir: Path, rel: str) -> LocalFile:
    """The guest file `rel` as the walk would list it; `FileNotFoundError` unless it is a plain contained file."""
    try:
        info = await anyio.to_thread.run_sync(lstat_guest_path, agent_dir, rel)
    except OSError as exc:
        if unsettled(exc):
            raise FileNotFoundError(f"No guest file at {rel}") from exc
        raise
    if not stat.S_ISREG(info.st_mode):
        raise FileNotFoundError(f"No guest file at {rel}")
    return LocalFile(rel, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


async def _sync_one(agent_id: UUID, agent_dir: Path, store: StorageBackend, rel: str) -> SyncedFile:
    generation, _ = await anyio.to_thread.run_sync(read_or_create_generation, agent_dir)
    local = await _local_file(agent_dir, rel)
    key = agent_storage_key(agent_id, rel)
    version = await store.get_version(key)
    remote = Remote(version.etag, version.size, version.modified_at) if version.exists and not version.is_dir else None
    manifest = await workspace_mirror_entry_dao.list_for_agent(agent_id)
    entry = manifest.get(rel)
    base = entry if entry is not None and entry.generation == generation else None
    if remote is None and recorded_count(manifest, generation) > 0 and await stores_nothing(store, agent_id):
        raise MirrorBusyError(f"Storage lists nothing for agent {agent_id} although the mirror recorded files there")
    now_ns = _wall_clock_ns()
    reason = freeze_reason(local, remote, base, now_ns=now_ns, settle_local=False)
    if reason is not None:
        raise MirrorBusyError(f"{rel} cannot be synced yet: {reason}")
    action = decide(rel, base, local, remote, now=moment(now_ns), retry_too_large=True)
    if action is Action.DELETE_LOCAL:
        # Guest deletes are left to a pass, where the mass-delete guard can hold them back.
        raise FileNotFoundError(f"{rel} was deleted from storage")
    work = PathWork(agent_id, agent_dir, store, generation, rel, base, local, remote)
    try:
        done = await run(action, work, moment(now_ns))
    except FrozenPathError as exc:
        raise MirrorBusyError(f"{rel} changed while it was being synced") from exc
    ledger = _Ledger(agent_id, ReconcileReport())
    await ledger.apply(rel, done)
    await ledger.forget(set())
    await ledger.invalidate()
    match done.outcome:
        case Outcome.CONFLICT if done.conflict_path is not None:
            raise MirrorConflictError(rel, done.conflict_path)
        case Outcome.TOO_LARGE:
            raise done.refusal or StorageFileTooLargeError(MAX_FILE_BYTES, local.size)
        case Outcome.RACED:
            raise MirrorBusyError(f"{rel} changed while it was being synced")
        case _:
            return SyncedFile(rel, key, done.size_bytes)


async def sync_path(agent: AgentRecord, rel: str) -> SyncedFile:
    """Share-link entry: settle one guest file with storage now and return what storage holds for it."""
    if not is_shareable(rel):
        raise ValueError(f"Not a shareable workspace path: {rel!r}")
    store = durable_store()
    if store is None:
        raise ValueError("sync_path needs durable agent storage; local mode serves guest files directly")
    agent_dir = agent_state_dir(agent.id)
    try:
        async with guest_write_section(agent.id), mirror_lock(agent_dir, wait_seconds=SYNC_LOCK_WAIT_SECONDS):
            return await _sync_one(agent.id, agent_dir, store, rel)
    except WorkspaceLockTimeoutError as exc:
        raise MirrorBusyError("The agent workspace is busy") from exc
