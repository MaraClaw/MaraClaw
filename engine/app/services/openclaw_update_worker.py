"""Worker-only loop that drives pending and interrupted OpenClaw upgrade runs."""

from __future__ import annotations

from typing import Final

import anyio
from anyio import to_thread
from psycopg import Error as PsycopgError
from pywhales import DockerClient

from app.core.logging import logger, new_trace_id
from app.core.row_memo import clear_row_memo
from app.db.errors import DbError
from app.runtime.tasks import LoopProgress, capped_backoff, loop_progress
from app.services.agent_manager import agent_manager
from app.services.openclaw_update_docker import UpgradeDocker
from app.services.openclaw_update_executor import UpgradeExecutor
from app.services.openclaw_update_store import PostgresUpgradeStore, UpgradeStore

POLL_SECONDS = 5.0
ARTIFACT_SWEEP_SECONDS = 60.0
# Readiness fails when no pass finished (or idle check passed) this long; time inside ``executor.process`` is exempt.
PROGRESS_BUDGET_SECONDS: Final = 300.0
DOCKER_RETRY_CAP_SECONDS: Final = 60.0


async def drive_active_runs(store: UpgradeStore, executor: UpgradeExecutor, progress: LoopProgress) -> int:
    """Process every active run once; one failing run never starves the others."""
    runs = await store.list_active()
    for run in runs:
        new_trace_id()
        clear_row_memo()
        try:
            # A cutover has its own contract and is never wrapped in a deadline or cancelled for staleness.
            with progress.exempt():
                await executor.process(run.id)
        except Exception:
            logger.exception("[OpenClawUpdate] run crashed run={} agent={}", run.id, run.agent_id)
    return len(runs)


async def _acquire_docker(store: UpgradeStore, progress: LoopProgress) -> DockerClient:
    """Install the shared client on ``agent_manager`` with capped backoff.

    Idle without Docker is healthy (a dev box): it beats. Waiting while runs are active, or
    unable to tell, does not beat, so readiness fails once the budget lapses.
    """
    failures = 0
    while (client := agent_manager.docker_client) is None:
        client = await to_thread.run_sync(agent_manager.reacquire_docker_client)
        if client is not None:
            break
        try:
            stalled = bool(await store.list_active())
        except DbError, PsycopgError, OSError:
            logger.warning("[OpenClawUpdate] Docker unavailable and active runs unreadable; waiting")
            stalled = True
        else:
            if stalled:
                logger.warning("[OpenClawUpdate] Docker unavailable; active upgrade runs will not progress")
        if not stalled:
            progress.beat()
        await anyio.sleep(capped_backoff(failures, base_seconds=POLL_SECONDS, cap_seconds=DOCKER_RETRY_CAP_SECONDS))
        failures += 1
    return client


async def start_openclaw_update_worker() -> None:
    progress = loop_progress()
    store = PostgresUpgradeStore()
    client = await _acquire_docker(store, progress)
    executor = UpgradeExecutor(store, UpgradeDocker(client), agent_manager)
    async with anyio.create_task_group() as group:
        group.start_soon(_sweep_success_artifacts, executor)
        while True:
            try:
                _ = await drive_active_runs(store, executor, progress)
                progress.beat()
            except Exception:
                logger.exception("[OpenClawUpdate] worker pass failed")
            await anyio.sleep(POLL_SECONDS)


async def _sweep_success_artifacts(executor: UpgradeExecutor) -> None:
    """Run at startup and independently of active polling, including while a cutover holds a lock."""
    while True:
        new_trace_id()
        clear_row_memo()
        try:
            await executor.reconcile_success_artifacts()
        except Exception:
            logger.exception("[OpenClawUpdate] succeeded-artifact sweep failed")
        await anyio.sleep(ARTIFACT_SWEEP_SECONDS)
