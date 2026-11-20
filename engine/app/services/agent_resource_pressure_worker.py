"""Periodic sampling of every tier-governed agent's guest memory, CPU and managed storage (``agent_resource_pressure``)."""

from typing import Final
from uuid import UUID

from anyio import create_task_group, sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.agent_resource_pressure import GuestReads, close_orphaned_episodes, sample_page
from app.services.agent_resource_pressure_docker import DOCKER_TIMEOUT_SECONDS
from app.services.agent_resource_pressure_events import read_oom_events, record_oom_event

PAGE_SIZE: Final = 50
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0
# The loop beats after every page, so the longest silence is the interval sleep followed by the next pass's orphan
# sweep, one page read and one sampled page. One statement waits at most the pool checkout plus the statement
# timeout, 60 seconds at the defaults.
ORPHAN_SWEEP_SECONDS: Final = 60.0
PAGE_READ_SECONDS: Final = 60.0
# One inspect and one stats batch, each held to its own timeout; a call with no answer latches the pass, so a later
# page makes neither.
PAGE_DOCKER_SECONDS: Final = 2 * DOCKER_TIMEOUT_SECONDS
# A healthy page issues a handful of batch statements and at most a write or two per agent, each in milliseconds;
# this is room for a slow database, not the sum of every statement's worst case.
PAGE_DATABASE_SECONDS: Final = 600.0


def progress_budget_seconds() -> float:
    return (
        get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS
        + ORPHAN_SWEEP_SECONDS
        + PAGE_READ_SECONDS
        + PAGE_DOCKER_SECONDS
        + PAGE_DATABASE_SECONDS
    )


async def start_agent_resource_pressure_worker() -> None:
    """Run the sampling loop with the long-lived ``docker events`` OOM reader beside it.

    The reader is a child of this task: it starts with the loop, is cancelled and reaped with it, never beats, and
    never raises, so a slow or failing reader cannot make the worker unready or hold up a pass.
    """
    async with create_task_group() as group:
        group.start_soon(read_oom_events, record_oom_event)
        await _sample_forever()


async def _sample_forever() -> None:
    """Sample every candidate agent in id order, then sleep the interval; beat after every completed page.

    A pass first closes the episodes of agents that no longer exist. One page's failure is logged and the pass
    continues, and that page still beats. All pages of a pass share one ``GuestReads``, so a Docker call with no answer
    costs the pass one wait; the next pass asks again. A database error reading a page is retried in place with
    capped backoff, resumes from the same cursor and never beats.
    """
    progress = loop_progress()
    failures = 0
    cursor: UUID | None = None
    reads = GuestReads()
    starting = True
    new_trace_id()
    while True:
        if starting:
            starting = False
            try:
                _ = await close_orphaned_episodes()
            except Exception as exc:
                logger.bind(exception_type=type(exc).__name__).warning("[ResourcePressure] closing orphans failed")
        try:
            page = await agent_dao.list_workspace_quota_page(cursor, PAGE_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[ResourcePressure] listing agents failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        try:
            await sample_page(page, reads)
        except Exception as exc:
            logger.bind(exception_type=type(exc).__name__).warning("[ResourcePressure] sampling a page failed")
        progress.beat()
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        await sleep(get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS)
        reads = GuestReads()
        new_trace_id()
        starting = True
