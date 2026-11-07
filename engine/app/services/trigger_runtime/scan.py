"""Bounded, owned concurrent trigger evaluation for the daemon tick."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from functools import partial

from app.core.logging import logger
from app.dao.agent_dao import agent_dao
from app.dao.tenant_dao import tenant_dao
from app.records.trigger import AgentTriggerRecord
from app.runtime.tasks import cancel_tasks
from app.services.timezone_utils import get_agent_timezone_sync

MAX_CONCURRENT_EVALUATIONS = 8
"""Well below the shared database pool (``DATABASE_POOL_MAX_SIZE``), which every worker loop draws from."""

type Evaluate = Callable[[AgentTriggerRecord], Awaitable[None]]
type ReadPage = Callable[[uuid.UUID | None, int], Awaitable[Sequence[AgentTriggerRecord]]]
"""Enabled triggers in id order after the given id, at most the given count."""


class TriggerScanner:
    """Walks the enabled triggers in id-ordered pages, never owning more than ``limit`` evaluation tasks.

    The cursor persists across ticks and wraps to the start at the end of the table. A page is never larger than the
    free slots, so a trigger the tick had no capacity or time for stays ahead of the cursor and is evaluated on a
    later tick; a hung evaluation cannot starve the triggers behind it. A tick waits only ``settle`` seconds in total
    for what it started, so a slow poll never delays the claim phase. That evaluation keeps running in its own task
    and the cursor skips its trigger until it finishes. ``close`` cancels and awaits every task, so nothing outlives
    the loop that owns the scanner.
    """

    def __init__(self, limit: int = MAX_CONCURRENT_EVALUATIONS) -> None:
        self._limit: int = limit
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._cursor: uuid.UUID | None = None

    async def scan(self, read_page: ReadPage, evaluate: Evaluate, *, settle: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle
        while (free := self._limit - len(self._tasks)) > 0:
            page = await read_page(self._cursor, free)
            exhausted = len(page) < free
            self._cursor = None if exhausted else page[-1].id
            wave = [self._start(trigger, evaluate) for trigger in page if trigger.id not in self._tasks]
            if wave:
                _ = await asyncio.wait(wave, timeout=max(0.0, deadline - loop.time()))
            if exhausted or loop.time() >= deadline:
                return

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        await cancel_tasks(tasks)
        self._tasks.clear()
        self._cursor = None

    def _start(self, trigger: AgentTriggerRecord, evaluate: Evaluate) -> asyncio.Task[None]:
        # An empty context: concurrent evaluations must never inherit and share a caller's connection.
        task = asyncio.create_task(self._run(trigger, evaluate), context=contextvars.Context())
        self._tasks[trigger.id] = task
        task.add_done_callback(partial(self._finished, trigger.id))
        return task

    def _finished(self, trigger_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(trigger_id) is task:
            del self._tasks[trigger_id]

    @staticmethod
    async def _run(trigger: AgentTriggerRecord, evaluate: Evaluate) -> None:
        try:
            await evaluate(trigger)
        except Exception as error:
            logger.warning(f"Error evaluating trigger {trigger.name}: {error}")


async def cron_agent_timezones(triggers: Iterable[AgentTriggerRecord]) -> dict[uuid.UUID, str]:
    """Resolve the agent timezone of every cron trigger that has none configured, in two queries for the tick.

    Without it each cron evaluation would read its agent and tenant itself, and fanning evaluation out would
    multiply those reads. Agents that no longer exist resolve to UTC, like ``get_agent_timezone``.
    """
    agent_ids = {
        trigger.agent_id
        for trigger in triggers
        if trigger.type == "cron" and not _configured_timezone(trigger.config.get("timezone"))
    }
    if not agent_ids:
        return {}
    zones = dict.fromkeys(agent_ids, "UTC")
    agents = await agent_dao.get_many(list(agent_ids))
    tenant_ids = list({agent.tenant_id for agent in agents if agent.tenant_id})
    tenants = {tenant.id: tenant for tenant in await tenant_dao.get_by_ids(tenant_ids)} if tenant_ids else {}
    for agent in agents:
        zones[agent.id] = get_agent_timezone_sync(agent, tenants.get(agent.tenant_id) if agent.tenant_id else None)
    return zones


def _configured_timezone(value: object) -> bool:
    return isinstance(value, str) and bool(value)
