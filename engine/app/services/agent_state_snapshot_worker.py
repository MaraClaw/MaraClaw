"""The `agent_state_snapshot` loop: keep each agent's changed OpenClaw state as its one current S3 snapshot."""

import shutil
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Final
from uuid import UUID

from anyio import sleep, to_thread
from fastapi import HTTPException
from psycopg import Error as PsycopgError
from pywhales import ClientNotFoundError, DockerClient
from pywhales.exceptions import DockerException, NoSuchContainer

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.core.row_memo import memo_drop
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, setup_workspace_lock
from app.services.agent_state_snapshot import (
    ARCHIVE_NAME,
    CONTAINER_TMP,
    GUEST_STATE_DIR,
    ONE_OFF_TIMEOUT_SECONDS,
    ROLE_LABEL,
    STORAGE_ERRORS,
    WALK_SECONDS,
    SnapshotBusyError,
    SnapshotName,
    has_host_state,
    list_snapshots,
    new_snapshot_name,
    new_work_dir,
    run_one_off,
    snapshot_lock,
    snapshot_store,
    state_digest,
    tenant_token,
    work_root,
)
from app.services.agent_state_snapshot_archive import (
    CHECK_SECONDS,
    SnapshotRejectedError,
    check_backup_report,
    inspect_archive,
)
from app.services.company_transfer_runtime_gate import assert_transfer_ready
from app.services.docker_capacity import DockerBusyError, DockerPriority, run_docker
from app.services.openclaw_update_gate import OpenClawUpdateGateError, assert_no_active_run
from app.services.storage_runtime.base import StorageBackend, WriteCondition
from app.services.storage_runtime.upload_source import open_upload_source
from app.services.workspace_mirror import agent_state_dir, agents_root

PAGE_SIZE: Final = 50
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0
PAGE_READ_SECONDS: Final = 60.0
# Room for the listing that seeds the digest cache, one upload of an archive at the size limit, and the prune.
TRANSFER_SECONDS: Final = 600.0
# The loop beats after every agent, one at a time, so one agent bounds the silence after a page read: two digest walks,
# the backup container, the archive checks and the transfers. A slower upload turns the loop stale without cancelling it.
AGENT_SECONDS: Final = 2 * WALK_SECONDS + ONE_OFF_TIMEOUT_SECONDS + CHECK_SECONDS + TRANSFER_SECONDS
STARVED_INTERVALS: Final = 3
CREATE_ROLE: Final = "create"
_BACKUP_SCRIPT: Final = (
    "exec timeout -s KILL 120 openclaw backup create --no-include-workspace --verify --json "
    f"--output /out/{ARCHIVE_NAME} 2>/out/backup.log"
)
_GUEST_ENVS: Final = {
    "HOME": "/home/node",
    "OPENCLAW_HOME": "/home/node",
    "OPENCLAW_STATE_DIR": GUEST_STATE_DIR,
    "OPENCLAW_CONFIG_PATH": f"{GUEST_STATE_DIR}/openclaw.json",
    "TMPDIR": CONTAINER_TMP,
}
_monotonic = time.monotonic


def progress_budget_seconds() -> float:
    return get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS + PAGE_READ_SECONDS + AGENT_SECONDS


class SnapshotOutcome(StrEnum):
    NO_STATE = "no_state"
    UNCHANGED = "unchanged"
    BUSY = "busy"
    INELIGIBLE = "ineligible"
    FAILED = "failed"
    REJECTED = "rejected"
    PUBLISHED = "published"


@dataclass(slots=True)
class SnapshotTracker:
    """What one worker process remembers: each agent's last published digest, and since when it has lacked one."""

    digests: dict[UUID, str] = field(default_factory=dict)
    behind_since: dict[UUID, float] = field(default_factory=dict)

    async def published_digest(self, store: StorageBackend, agent_id: UUID) -> str:
        """The digest of the agent's newest snapshot; a miss is seeded by one listing, an empty string when none."""
        if agent_id not in self.digests:
            newest = (await list_snapshots(store, agent_id)).newest()
            self.digests[agent_id] = "" if newest is None else newest.digest
        return self.digests[agent_id]

    def current(self, agent_id: UUID, digest: str | None = None) -> None:
        if digest is not None:
            self.digests[agent_id] = digest
        _ = self.behind_since.pop(agent_id, None)

    def behind(self, agent_id: UUID) -> None:
        """Note a pass that left the agent without a current snapshot; warn once that has lasted three intervals."""
        now = _monotonic()
        since = self.behind_since.setdefault(agent_id, now)
        if now - since > STARVED_INTERVALS * get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS:
            logger.warning("[StateSnapshot] agent={} has had no current snapshot for {:.0f}s", agent_id, now - since)


async def start_agent_state_snapshot_worker() -> None:
    """Snapshot each changed agent page by page, one agent at a time, then sleep the interval.

    It beats after every agent and every page, a logged agent failure included. A database error reading a page is
    retried in place with capped backoff and never beats. Without a store to snapshot into (local storage, the mirror
    or snapshots disabled) or without Docker, the loop keeps beating and sleeping without paging.
    """
    progress = loop_progress()
    tracker = SnapshotTracker()
    failures = 0
    cursor: UUID | None = None
    new_trace_id()
    await remove_leftovers(agent_manager.docker_client)
    while True:
        client = agent_manager.docker_client
        if snapshot_store() is None or client is None:
            progress.beat()
            await sleep(get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS)
            continue
        try:
            page = await agent_dao.list_workspace_mirror_page(cursor, PAGE_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[StateSnapshot] listing agents failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        for agent in page:
            await _snapshot(client, agent, tracker)
            progress.beat()
        progress.beat()
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        await sleep(get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS)
        new_trace_id()


async def remove_leftovers(client: DockerClient | None) -> None:
    """Remove the backup containers and work directories an earlier worker process left behind; restores are not touched."""
    if client is not None:
        try:
            await run_docker(_remove_backup_containers, client, priority=DockerPriority.BACKGROUND)
        except (DockerBusyError, ClientNotFoundError, DockerException) as exc:
            logger.bind(exception_type=type(exc).__name__).warning("[StateSnapshot] leftover backups were not removed")
    await to_thread.run_sync(partial(shutil.rmtree, work_root(agents_root()) / CREATE_ROLE, ignore_errors=True))


def _remove_backup_containers(client: DockerClient) -> None:
    for listed in client.container.list(all=True, filters=[("label", f"{ROLE_LABEL}={CREATE_ROLE}")]):
        with suppress(NoSuchContainer):
            client.container.remove(listed.id, force=True)


async def _snapshot(client: DockerClient, agent: AgentRecord, tracker: SnapshotTracker) -> None:
    try:
        outcome = await snapshot_agent(client, agent, tracker)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[StateSnapshot] snapshot failed agent={}", agent.id)
        outcome = SnapshotOutcome.FAILED
    if outcome not in (SnapshotOutcome.NO_STATE, SnapshotOutcome.UNCHANGED, SnapshotOutcome.PUBLISHED):
        tracker.behind(agent.id)


async def snapshot_agent(client: DockerClient, agent: AgentRecord, tracker: SnapshotTracker) -> SnapshotOutcome:
    """Publish a new snapshot of the agent when its state changed and no turn is running; the current one stays otherwise.

    Lock order is the exclusive setup lock, taken without waiting, then the snapshot flock. The backup runs under both;
    the checks, the upload and the prune run under the snapshot flock alone, so turns wait only for the backup.
    """
    store = snapshot_store()
    agent_dir = agent_state_dir(agent.id)
    if store is None or not await to_thread.run_sync(has_host_state, agent_dir):
        return SnapshotOutcome.NO_STATE
    tenant = tenant_token(agent.tenant_id)
    digest = await to_thread.run_sync(state_digest, agent_dir, tenant)
    if digest == await tracker.published_digest(store, agent.id):
        tracker.current(agent.id)
        return SnapshotOutcome.UNCHANGED
    out: Path | None = None
    try:
        async with AsyncExitStack() as held:
            async with setup_workspace_lock(agent_dir, timeout_seconds=0):
                await held.enter_async_context(snapshot_lock(agent_dir))
                fresh = await _eligible(agent.id, tenant, agent_dir)
                if fresh is None:
                    return SnapshotOutcome.INELIGIBLE
                out = await to_thread.run_sync(_new_out_dir, agent_dir.parent)
                stdout = await run_docker(_backup, client, fresh, agent_dir, out, priority=DockerPriority.BACKGROUND)
                # Taken after the backup so its own bookkeeping row in state/openclaw.sqlite is not a change.
                digest = await to_thread.run_sync(state_digest, agent_dir, tenant)
            return await _publish(store, agent.id, tenant, digest, stdout, out, tracker)
    except WorkspaceLockTimeoutError, SnapshotBusyError, DockerBusyError:
        return SnapshotOutcome.BUSY
    except DockerException as exc:
        logger.bind(exception_type=type(exc).__name__).warning(
            "[StateSnapshot] backup container failed agent={} exit_code={}", agent.id, exc.return_code
        )
        return SnapshotOutcome.FAILED
    finally:
        if out is not None:
            await to_thread.run_sync(partial(shutil.rmtree, out.parent, ignore_errors=True))


async def _eligible(agent_id: UUID, tenant: str, agent_dir: Path) -> AgentRecord | None:
    """The agent's row re-read under both locks, or None when the agent must not be snapshotted now.

    A deleted agent has no row; its deletion cleanup only runs once the row is gone.
    """
    memo_drop("agent", agent_id)
    fresh = await agent_dao.get(agent_id)
    if fresh is None or tenant_token(fresh.tenant_id) != tenant:
        return None
    try:
        await assert_no_active_run(agent_id)
        await assert_transfer_ready(agent_id)
    except OpenClawUpdateGateError, HTTPException:
        return None
    return fresh if await to_thread.run_sync(has_host_state, agent_dir) else None


def _new_out_dir(agents: Path) -> Path:
    out = new_work_dir(agents, CREATE_ROLE) / "out"
    out.mkdir()
    return out


def _backup(client: DockerClient, agent: AgentRecord, agent_dir: Path, out: Path) -> str:
    return run_one_off(
        client,
        agent.openclaw_image_id or get_settings().MARACLAW_OPENCLAW_IMAGE,
        _BACKUP_SCRIPT,
        name=f"maraclaw-state-snapshot-{agent.id}",
        role=CREATE_ROLE,
        agent_id=agent.id,
        mounts={agent_dir: GUEST_STATE_DIR, out: "/out"},
        envs=_GUEST_ENVS,
    )


def _inspect(out: Path) -> None:
    scratch = out.parent / "inspect"
    scratch.mkdir()
    inspect_archive(out / ARCHIVE_NAME, scratch, get_settings().AGENT_STATE_SNAPSHOT_MAX_BYTES)


async def _publish(
    store: StorageBackend,
    agent_id: UUID,
    tenant: str,
    digest: str,
    stdout: str,
    out: Path,
    tracker: SnapshotTracker,
) -> SnapshotOutcome:
    try:
        check_backup_report(stdout)
        await to_thread.run_sync(_inspect, out)
    except SnapshotRejectedError as exc:
        logger.warning("[StateSnapshot] discarded the snapshot of agent={}: {}", agent_id, exc)
        return SnapshotOutcome.REJECTED
    memo_drop("agent", agent_id)
    fresh = await agent_dao.get(agent_id)
    if fresh is None or tenant_token(fresh.tenant_id) != tenant:
        return SnapshotOutcome.INELIGIBLE
    name = new_snapshot_name(digest, tenant, datetime.now(UTC))
    source = await to_thread.run_sync(partial(open_upload_source, out / ARCHIVE_NAME, with_md5=True))
    try:
        result = await store.write_source_if_match(
            name.key(agent_id), source, condition=WriteCondition(require_absent=True), content_type="application/gzip"
        )
    finally:
        source.close()
    if not result.ok:
        logger.warning("[StateSnapshot] snapshot key already taken agent={} key={}", agent_id, name.file_name)
        return SnapshotOutcome.FAILED
    tracker.current(agent_id, digest)
    await _prune(store, agent_id, name)
    logger.info("[StateSnapshot] published agent={} snapshot={} bytes={}", agent_id, name.file_name, source.size)
    return SnapshotOutcome.PUBLISHED


async def _prune(store: StorageBackend, agent_id: UUID, current: SnapshotName) -> None:
    """Delete the snapshots older than `current` that carry no held marker; a failure leaves them for the next prune."""
    try:
        for old in (await list_snapshots(store, agent_id)).superseded(current):
            await store.delete(old.key(agent_id))
    except STORAGE_ERRORS as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[StateSnapshot] pruning failed agent={}", agent_id)
