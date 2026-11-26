"""Periodic measurement of every tier-governed agent's workspace disk usage (``agent_workspace_quota``)."""

from collections import deque
from collections.abc import Awaitable, Callable, Sequence, Set as AbstractSet
from math import ceil
from typing import Final
from uuid import UUID

from anyio import create_task_group, sleep
from psycopg import Error as PsycopgError

from app.config import get_settings
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.db.errors import DbError
from app.records.agent import AgentRecord
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
# The loop beats after every page, so the longest silence is the interval sleep followed by one page read and one page
# of agents measured by the lanes. One statement waits at most the pool checkout plus the statement timeout, 60
# seconds at the defaults.
PAGE_READ_SECONDS: Final = 60.0
# The walk is held by its deadline inside its thread and the layer read by its subprocess timeout. The database
# share is room for a slow database around the measurement's few short statements.
AGENT_DATABASE_SECONDS: Final = 60.0
AGENT_SECONDS: Final = WALK_DEADLINE_SECONDS + LAYER_INSPECT_TIMEOUT_SECONDS + AGENT_DATABASE_SECONDS

type PageReader = Callable[[UUID | None, int], Awaitable[Sequence[AgentRecord]]]


def progress_budget_seconds() -> float:
    """The interval, one page read and one page of agents each at their bound.

    A lane takes the next waiting agent the moment it frees, so every lane is busy until the page runs out and the
    page ends within ``ceil(PAGE_SIZE / concurrency)`` agents at their bound, whatever each one takes.
    """
    settings = get_settings()
    rounds = ceil(PAGE_SIZE / settings.AGENT_WORKSPACE_QUOTA_CONCURRENCY)
    return settings.AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS + PAGE_READ_SECONDS + rounds * AGENT_SECONDS


async def start_agent_workspace_quota_worker() -> None:
    """Measure every agent, then sleep the interval; beat after every completed page.

    A pass first pages the agents whose usage row asks to be measured first (a failed directory walk, or a request
    path that handed its re-measure over), then every other agent in id order. Up to
    ``AGENT_WORKSPACE_QUOTA_CONCURRENCY`` agents of a page are measured at once. One agent's failure is logged and the pass
    continues, and its page still beats. A database error reading a page is retried in place with capped backoff,
    resumes from the same cursor and never beats. Each pass shares one ``LayerReads``, so a stalled Docker daemon costs
    the pass one layer-read timeout per lane instead of one per agent.
    """
    progress = loop_progress()
    new_trace_id()
    while True:
        layers = LayerReads()
        first = await _sweep(agent_dao.list_workspace_quota_priority_page, layers, progress.beat, skip=frozenset())
        _ = await _sweep(agent_dao.list_workspace_quota_page, layers, progress.beat, skip=first)
        await sleep(get_settings().AGENT_WORKSPACE_QUOTA_INTERVAL_SECONDS)
        new_trace_id()


async def _sweep(
    read: PageReader, layers: LayerReads, beat: Callable[[], None], *, skip: AbstractSet[UUID]
) -> set[UUID]:
    """Measure every agent ``read`` pages except ``skip``, a page at a time; returns the ids it paged."""
    paged: set[UUID] = set()
    cursor: UUID | None = None
    while True:
        page = await _read_page(read, cursor)
        await _measure_page([agent for agent in page if agent.id not in skip], layers)
        beat()
        paged.update(agent.id for agent in page)
        if len(page) < PAGE_SIZE:
            return paged
        cursor = page[-1].id


async def _read_page(read: PageReader, cursor: UUID | None) -> Sequence[AgentRecord]:
    failures = 0
    while True:
        try:
            return await read(cursor, PAGE_SIZE)
        except (DbError, PsycopgError, OSError) as exc:
            logger.warning("[WorkspaceQuota] listing agents failed; will retry: {}", type(exc).__name__)
            await sleep(capped_backoff(failures, base_seconds=DB_RETRY_BASE_SECONDS, cap_seconds=DB_RETRY_CAP_SECONDS))
            failures += 1


async def _measure_page(page: Sequence[AgentRecord], layers: LayerReads) -> None:
    """Measure ``page`` with a fixed number of lanes, each one agent at a time taking the next waiting agent."""
    waiting = deque(page)

    async def lane() -> None:
        while waiting:
            agent = waiting.popleft()
            try:
                await measure_agent(agent, layers)
            except Exception as exc:
                logger.bind(exception_type=type(exc).__name__).warning(
                    "[WorkspaceQuota] measuring failed agent={}", agent.id
                )

    async with create_task_group() as group:
        for _ in range(min(get_settings().AGENT_WORKSPACE_QUOTA_CONCURRENCY, len(waiting))):
            group.start_soon(lane)
