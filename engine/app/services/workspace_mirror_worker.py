"""Periodic two-way mirror of every OpenClaw guest's state directory with durable agent storage (``workspace_mirror``)."""

from typing import Final
from uuid import UUID

from anyio import Path as AsyncPath, sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.records.agent import AgentRecord
from app.runtime.tasks import capped_backoff, loop_progress
from app.services.workspace_mirror import PASS_BUDGET_SECONDS, agent_state_dir, durable_store, mirror_agent

PAGE_SIZE: Final = 50
DB_RETRY_BASE_SECONDS: Final = 1.0
DB_RETRY_CAP_SECONDS: Final = 30.0
# The loop beats after every page, so the longest silence is the interval sleep followed by one page read and a page
# of agents each mirrored at its bound. One statement waits at most the pool checkout plus the statement timeout, 60
# seconds at the defaults.
PAGE_READ_SECONDS: Final = 60.0
# A pass stops scheduling operations once its budget has passed. The share on top is room for the operation in flight
# at that moment, one object transfer, and for the lock and gate statements around the pass.
AGENT_OVERRUN_SECONDS: Final = 120.0
AGENT_SECONDS: Final = PASS_BUDGET_SECONDS + AGENT_OVERRUN_SECONDS


def progress_budget_seconds() -> float:
    return get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS + PAGE_READ_SECONDS + PAGE_SIZE * AGENT_SECONDS


async def start_workspace_mirror_worker() -> None:
    """Mirror every agent in id order, then sleep the interval; beat after every completed page.

    One agent's failure is logged and the pass continues, and its page still beats. A database error reading a page
    is retried in place with capped backoff, resumes from the same cursor and never beats. When there is no durable
    store to mirror into (local storage, or the mirror disabled) the loop keeps beating and sleeping without paging.
    """
    progress = loop_progress()
    failures = 0
    cursor: UUID | None = None
    new_trace_id()
    while True:
        if durable_store() is None:
            progress.beat()
            await sleep(get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS)
            continue
        try:
            page = await agent_dao.list_workspace_mirror_page(cursor, PAGE_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[WorkspaceMirror] listing agents failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1
            continue
        failures = 0
        for agent in page:
            await _mirror(agent)
        progress.beat()
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        await sleep(get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS)
        new_trace_id()


async def _mirror(agent: AgentRecord) -> None:
    try:
        if await AsyncPath(agent_state_dir(agent.id)).is_dir():
            _ = await mirror_agent(agent)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[WorkspaceMirror] mirroring failed agent={}", agent.id)
