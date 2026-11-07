"""Bounded, owned concurrent trigger evaluation for the daemon tick."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Iterable
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


class TriggerScanner:
    """Runs one owned task per trigger under a fixed concurrency bound.

    A tick waits only ``settle`` seconds for its evaluations, so a slow poll never delays the claim phase. It keeps
    running in its own task and the next tick skips that trigger until it finishes. ``close`` cancels and awaits
    every task, so nothing outlives the loop that owns the scanner.
    """

    def __init__(self, limit: int = MAX_CONCURRENT_EVALUATIONS) -> None:
        self._limit: int = limit
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._gate: asyncio.Semaphore | None = None
        self._gate_loop: asyncio.AbstractEventLoop | None = None

    async def scan(self, triggers: Iterable[AgentTriggerRecord], evaluate: Evaluate, *, settle: float) -> None:
        started = [self._start(trigger, evaluate) for trigger in triggers if trigger.id not in self._tasks]
        if started:
            _ = await asyncio.wait(started, timeout=settle)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        await cancel_tasks(tasks)
        self._tasks.clear()
        self._gate = self._gate_loop = None

    def _start(self, trigger: AgentTriggerRecord, evaluate: Evaluate) -> asyncio.Task[None]:
        # An empty context: concurrent evaluations must never inherit and share a caller's connection.
        task = asyncio.create_task(self._run(trigger, evaluate), context=contextvars.Context())
        self._tasks[trigger.id] = task
        task.add_done_callback(partial(self._finished, trigger.id))
        return task

    def _finished(self, trigger_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(trigger_id) is task:
            del self._tasks[trigger_id]

    async def _run(self, trigger: AgentTriggerRecord, evaluate: Evaluate) -> None:
        async with self._semaphore():
            try:
                await evaluate(trigger)
            except Exception as error:
                logger.warning(f"Error evaluating trigger {trigger.name}: {error}")

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._gate is None or self._gate_loop is not loop:
            self._gate = asyncio.Semaphore(self._limit)
            self._gate_loop = loop
        return self._gate


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
