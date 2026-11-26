"""Bounded, owned concurrent trigger evaluation for the daemon tick."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from functools import partial

from app.core.logging import logger
from app.records.trigger import DueTrigger
from app.runtime.tasks import cancel_tasks

MAX_CONCURRENT_EVALUATIONS = 8
"""Well below the shared database pool (``DATABASE_POOL_MAX_SIZE``), which every worker loop draws from."""

type Evaluate = Callable[[DueTrigger], Awaitable[None]]
type Cursor = tuple[datetime, uuid.UUID]
"""The (``next_due_at``, id) position of a due trigger."""
type ReadPage = Callable[[Cursor | None, int], Awaitable[Sequence[DueTrigger]]]
"""Due triggers in (``next_due_at``, id) order after the given position, at most the given count."""


class TriggerScanner:
    """Walks the due triggers in (due time, id) pages, never owning more than ``limit`` evaluation tasks.

    The cursor persists across ticks and wraps to the start when a page comes back short. A page is never larger than
    the free slots, so a trigger the tick had no capacity or time for stays ahead of the cursor and is evaluated on a
    later tick; a hung evaluation cannot starve the triggers behind it. A tick waits only ``settle`` seconds in total
    for what it started, so a slow poll never delays the claim phase. That evaluation keeps running in its own task
    and the cursor skips its trigger until it finishes. ``close`` cancels and awaits every task, so nothing outlives
    the loop that owns the scanner.
    """

    def __init__(self, limit: int = MAX_CONCURRENT_EVALUATIONS) -> None:
        self._limit: int = limit
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._cursor: Cursor | None = None

    async def scan(self, read_page: ReadPage, evaluate: Evaluate, *, settle: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle
        while (free := self._limit - len(self._tasks)) > 0:
            page = await read_page(self._cursor, free)
            exhausted = len(page) < free
            self._cursor = None if exhausted else (page[-1].due_at, page[-1].trigger.id)
            wave = [self._start(due, evaluate) for due in page if due.trigger.id not in self._tasks]
            if wave:
                _ = await asyncio.wait(wave, timeout=max(0.0, deadline - loop.time()))
            if exhausted or loop.time() >= deadline:
                return

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        await cancel_tasks(tasks)
        self._tasks.clear()
        self._cursor = None

    def _start(self, due: DueTrigger, evaluate: Evaluate) -> asyncio.Task[None]:
        # An empty context: concurrent evaluations must never inherit and share a caller's connection.
        task = asyncio.create_task(self._run(due, evaluate), context=contextvars.Context())
        self._tasks[due.trigger.id] = task
        task.add_done_callback(partial(self._finished, due.trigger.id))
        return task

    def _finished(self, trigger_id: uuid.UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(trigger_id) is task:
            del self._tasks[trigger_id]

    @staticmethod
    async def _run(due: DueTrigger, evaluate: Evaluate) -> None:
        try:
            await evaluate(due)
        except Exception as error:
            logger.warning(f"Error evaluating trigger {due.trigger.name}: {error}")
