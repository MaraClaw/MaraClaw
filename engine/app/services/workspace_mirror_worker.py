"""Periodic two-way mirror of every OpenClaw guest's state directory with durable agent storage (``workspace_mirror``)."""

from collections.abc import Callable, Sequence
from typing import Final
from uuid import UUID

from anyio import Condition, Path as AsyncPath, create_task_group, sleep
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
# The loop beats whenever an agent finishes and after every page. While agents wait, one of them is always running (the
# one holding its tenant), so the longest silence is the interval sleep, one page read and the first agent to finish,
# however many agents mirror at once. One statement waits at most the pool checkout plus the statement timeout, 60
# seconds at the defaults.
PAGE_READ_SECONDS: Final = 60.0
# An agent's pass starts its budget when its slot is granted and stops scheduling operations once it has passed. The
# share on top is room for the operation in flight at that moment, one object transfer, and for the lock and gate
# statements around the pass.
AGENT_OVERRUN_SECONDS: Final = 120.0
AGENT_SECONDS: Final = PASS_BUDGET_SECONDS + AGENT_OVERRUN_SECONDS


def progress_budget_seconds() -> float:
    return get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS + PAGE_READ_SECONDS + AGENT_SECONDS


async def start_workspace_mirror_worker() -> None:
    """Mirror every agent page by page, then sleep the interval; beat as each agent finishes and after every page.

    Up to `WORKSPACE_MIRROR_CONCURRENCY` agents of a page mirror at once, never two of the same tenant, each taking
    the next agent in id order whose tenant is free. One agent's failure is logged and the pass continues, and it
    still beats. A database error reading a page
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
        await _mirror_page(page, progress.beat)
        progress.beat()
        if len(page) == PAGE_SIZE:
            cursor = page[-1].id
            continue
        cursor = None
        await sleep(get_settings().WORKSPACE_MIRROR_INTERVAL_SECONDS)
        new_trace_id()


def _claim(waiting: list[AgentRecord], busy: set[UUID | None]) -> AgentRecord | None:
    """Take the first waiting agent whose tenant has none in flight, marking that tenant busy."""
    for agent in waiting:
        if agent.tenant_id not in busy:
            waiting.remove(agent)
            busy.add(agent.tenant_id)
            return agent
    return None


async def _mirror_page(page: Sequence[AgentRecord], beat: Callable[[], None]) -> None:
    """Mirror `page` with a fixed number of lanes, each one agent at a time and no tenant in two lanes at once.

    Tenants never overlap so one company's pushes do not queue behind each other on the quota ledger's lock timeout,
    and the number of lanes bounds how many transfer buffers exist at once.
    """
    waiting = list(page)
    busy: set[UUID | None] = set()
    freed = Condition()

    async def lane() -> None:
        while True:
            async with freed:
                agent = _claim(waiting, busy)
                while agent is None and waiting:
                    await freed.wait()
                    agent = _claim(waiting, busy)
                if agent is None:
                    return
            try:
                await _mirror(agent)
            finally:
                async with freed:
                    busy.discard(agent.tenant_id)
                    freed.notify_all()
            beat()

    async with create_task_group() as group:
        for _ in range(min(get_settings().WORKSPACE_MIRROR_CONCURRENCY, len(page))):
            group.start_soon(lane)


async def _mirror(agent: AgentRecord) -> None:
    try:
        if await AsyncPath(agent_state_dir(agent.id)).is_dir():
            _ = await mirror_agent(agent)
    except Exception as exc:
        logger.bind(exception_type=type(exc).__name__).warning("[WorkspaceMirror] mirroring failed agent={}", agent.id)
