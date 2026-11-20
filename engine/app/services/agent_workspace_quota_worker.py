"""Periodic measurement of every tier-governed agent's workspace disk usage (``agent_workspace_quota``)."""

from typing import Final
from uuid import UUID

from anyio import sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.agent_workspace_quota import (
    LAYER_INSPECT_TIMEOUT_SECONDS,
    WALK_DEADLINE_SECONDS,
    LayerReads,
    measure_agent,
)

PAGE_SIZE: Final = 50
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0
# The loop beats after every page, so the longest silence is the interval sleep followed by one page read and a page
# of agents each measured at its bound. One statement waits at most the pool checkout plus the statement timeout, 60
# seconds at the defaults.
PAGE_READ_SECONDS: Final = 60.0
# The walk is held by its deadline inside its thread and the layer read by its subprocess timeout. The database
# share is room for a slow database around the measurement's few short statements.
AGENT_DATABASE_SECONDS: Final = 60.0
AGENT_SECONDS: Final = WALK_DEADLINE_SECONDS + LAYER_INSPECT_TIMEOUT_SECONDS + AGENT_DATABASE_SECONDS


def progress_budget_seconds() -> float:
    return get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS + PAGE_READ_SECONDS + PAGE_SIZE * AGENT_SECONDS


async def start_agent_workspace_quota_worker() -> None:
    """Measure every agent in id order, then sleep the interval; beat after every completed page.

    One agent's failure is logged and the pass continues, and its page still beats. A database error reading a page
    is retried in place with capped backoff, resumes from the same cursor and never beats. Each pass shares one
    ``LayerReads``, so a stalled Docker daemon costs the pass one layer-read timeout instead of one per agent.
    """
    progress = loop_progress()
    failures = 0
    cursor: UUID | None = None
    layers = LayerReads()
    new_trace_id()
    while True:
        try:
            page = await agent_dao.list_workspace_quota_page(cursor, PAGE_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[WorkspaceQuota] listing agents failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        for agent in page:
            try:
                await measure_agent(agent, layers)
            except Exception as exc:
                logger.bind(exception_type=type(exc).__name__).warning(
                    "[WorkspaceQuota] measuring failed agent={}", agent.id
                )
        progress.beat()
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        await sleep(get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS)
        layers = LayerReads()
        new_trace_id()
