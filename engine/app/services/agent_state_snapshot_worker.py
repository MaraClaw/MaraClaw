"""The `agent_state_snapshot` loop: keep each agent's changed OpenClaw state as its one current S3 snapshot."""

import shutil
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
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
from app.dao.tenant_dao import tenant_dao
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.agent_manager import agent_manager
from app.services.agent_setup_lock import WorkspaceLockTimeoutError, setup_workspace_lock
from app.services.agent_state_snapshot import (
    ARCHIVE_NAME,
    ONE_OFF_TIMEOUT_SECONDS,
    ROLE_LABEL,
    STORAGE_ERRORS,
    WALK_SECONDS,
    SnapshotBusyError,
    SnapshotName,
    has_host_state,
    list_snapshots,
    new_snapshot_name,
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
from app.services.agent_state_snapshot_backup import CREATE_ROLE, new_out_dir, run_backup
from app.services.agent_state_snapshot_tracker import SnapshotOutcome, SnapshotTracker
from app.services.company_transfer_runtime_gate import assert_transfer_ready
from app.services.docker_capacity import DockerBusyError, DockerPriority, run_docker
from app.services.host_disk_headroom import require_headroom
from app.services.openclaw_update_gate import OpenClawUpdateGateError, assert_no_active_run
from app.services.storage_runtime.base import StorageBackend, WriteCondition
from app.services.storage_runtime.upload_source import open_upload_source
from app.services.workspace_mirror import agent_state_dir, agents_root
from app.services.workspace_mirror_scope import WalkLimitError

PAGE_SIZE: Final = 50
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0
PAGE_READ_SECONDS: Final = 60.0
# Room for the listing that seeds the digest cache, one upload of an archive at the size limit, and the prune.
TRANSFER_SECONDS: Final = 600.0
# The loop beats after every agent, one at a time, so one agent bounds the silence after a page read: two digest walks,
# the backup container, the archive checks and the transfers. A slower upload turns the loop stale without cancelling it.
AGENT_SECONDS: Final = 2 * WALK_SECONDS + ONE_OFF_TIMEOUT_SECONDS + CHECK_SECONDS + TRANSFER_SECONDS


def progress_budget_seconds() -> float:
    return get_settings().AGENT_STATE_SNAPSHOT_INTERVAL_SECONDS + PAGE_READ_SECONDS + AGENT_SECONDS


async def start_agent_state_snapshot_worker() -> None:
    """Snapshot due agents with changed state page by page, one agent at a time, then sleep the poll interval.

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
            tenant_ids = {agent.tenant_id for agent in page if agent.tenant_id is not None}
            tiers = await tenant_dao.tiers_for(sorted(tenant_ids))
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[StateSnapshot] reading agent page failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        for agent in page:
            tier = tiers.get(agent.tenant_id) if agent.tenant_id is not None else None
            # A vanished organization can race the page read. Skip its agents rather than apply tenantless policy.
            if agent.tenant_id is None or tier is not None:
                cadence = tier.state_snapshot_interval if tier is not None else None
                outcome = await snapshot_agent(client, agent, tracker, cadence=cadence)
                if outcome not in (
                    SnapshotOutcome.NO_STATE,
                    SnapshotOutcome.NOT_DUE,
                    SnapshotOutcome.UNCHANGED,
                    SnapshotOutcome.PUBLISHED,
                ):
                    tracker.behind(agent.id)
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


async def snapshot_agent(
    client: DockerClient, agent: AgentRecord, tracker: SnapshotTracker, *, cadence: timedelta | None
) -> SnapshotOutcome:
    """Run one pass over the agent unless its failure backoff is still running.

    The backoff is checked before the digest and before any lock. A FAILED pass, an error included, and a REJECTED one,
    a tree past the walk bound included, start or extend it; a BUSY or INELIGIBLE pass never does.
    """
    if tracker.backing_off(agent.id):
        return SnapshotOutcome.BACKING_OFF
    try:
        outcome = await _attempt(client, agent, tracker, cadence=cadence)
    except WalkLimitError as exc:
        logger.warning("[StateSnapshot] agent={} has more state than a restore can place: {}", agent.id, exc)
        outcome = SnapshotOutcome.REJECTED
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[StateSnapshot] snapshot failed agent={}", agent.id)
        outcome = SnapshotOutcome.FAILED
    if outcome in (SnapshotOutcome.FAILED, SnapshotOutcome.REJECTED):
        tracker.failed(agent.id)
    return outcome


async def _attempt(
    client: DockerClient, agent: AgentRecord, tracker: SnapshotTracker, *, cadence: timedelta | None
) -> SnapshotOutcome:
    """Publish a new snapshot of the agent when it is due, its state changed and no turn is running.

    Lock order is the exclusive setup lock, taken without waiting, then the snapshot flock. The backup runs under both;
    the checks, the upload and the prune run under the snapshot flock alone, so turns wait only for the backup. The host
    disk is checked before the backup's scratch directory exists, so a full disk fails the pass and writes nothing.
    """
    store = snapshot_store()
    agent_dir = agent_state_dir(agent.id)
    if store is None or not await to_thread.run_sync(has_host_state, agent_dir):
        return SnapshotOutcome.NO_STATE
    tenant = tenant_token(agent.tenant_id)
    previous = await tracker.published_snapshot(store, agent.id, tenant)
    if previous is not None and cadence is not None and datetime.now(UTC) - previous.created_at < cadence:
        tracker.current(agent.id)
        return SnapshotOutcome.NOT_DUE
    digest = await to_thread.run_sync(state_digest, agent_dir, tenant)
    if previous is not None and digest == previous.digest:
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
                await to_thread.run_sync(require_headroom, work_root(agent_dir.parent))
                out = await to_thread.run_sync(new_out_dir, agent_dir.parent)
                stdout = await run_docker(run_backup, client, fresh, agent_dir, out, priority=DockerPriority.BACKGROUND)
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
    tracker.current(agent_id, name)
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
