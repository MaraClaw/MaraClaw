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
from app.services.agent_resource_pressure import close_orphaned_episodes, sample_page
from app.services.agent_resource_pressure_events import read_oom_events, record_oom_event

PAGE_SIZE: Final = 50
# A pass samples one page at a time (one Docker inspect and stats batch each), so the readiness budget is the sleep
# between passes plus this allowance.
PASS_ALLOWANCE_SECONDS: Final = 3600.0
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0


def progress_budget_seconds() -> float:
    return get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS + PASS_ALLOWANCE_SECONDS


async def start_agent_resource_pressure_worker() -> None:
    """Run the sampling loop with the long-lived ``docker events`` OOM reader beside it.

    The reader is a child of this task: it starts with the loop, is cancelled and reaped with it, never beats, and
    never raises, so a slow or failing reader cannot make the worker unready or hold up a pass.
    """
    async with create_task_group() as group:
        group.start_soon(read_oom_events, record_oom_event)
        await _sample_forever()


async def _sample_forever() -> None:
    """Sample every candidate agent in id order, then sleep the interval; beat only after a completed pass.

    A pass first closes the episodes of agents that no longer exist. One page's failure is logged and the pass
    continues. A database error reading a page is retried in place with capped backoff, resumes from the same
    cursor and never beats.
    """
    progress = loop_progress()
    failures = 0
    cursor: UUID | None = None
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
            await sample_page(page)
        except Exception as exc:
            logger.bind(exception_type=type(exc).__name__).warning("[ResourcePressure] sampling a page failed")
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        progress.beat()
        await sleep(get_settings().AGENT_RESOURCE_PRESSURE_INTERVAL_SECONDS)
        new_trace_id()
        starting = True
