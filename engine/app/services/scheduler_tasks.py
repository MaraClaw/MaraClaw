"""After-commit schedule executions owned by the current runtime generation."""

import asyncio
from collections.abc import Callable, Coroutine
from contextvars import Context
from functools import partial

from app.config import get_settings
from app.core.incident_reporting import report_exception
from app.core.logging import logger, new_trace_id
from app.db.session import run_after_commit

_tasks: set[asyncio.Task[None]] = set()
_accepting = False
_generation = 0


def start_schedule_tasks() -> None:
    """Open admission for a new runtime, invalidating earlier commit callbacks."""
    global _accepting, _generation
    _generation += 1
    _accepting = True


def _completed(task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        new_trace_id()
        report_exception(error, source="scheduler", operation="schedule-execution")
        logger.opt(exception=error).error("Schedule execution failed")


def free_schedule_capacity() -> int:
    """Executions this worker can still start: the configured cap minus live tasks, zero while closed."""
    if not _accepting:
        return 0
    live = sum(not task.done() for task in _tasks)
    return max(0, get_settings().SCHEDULE_MAX_CONCURRENT_EXECUTIONS - live)


def schedule_execution(factory: Callable[[], Coroutine[object, object, None]]) -> asyncio.Future[bool]:
    """Launch lazily after commit, without inheriting request or transaction context.

    The future resolves True once the task started and False when it never will (closed or replaced runtime,
    no free capacity, or the owning transaction rolled back), so the caller can release what it claimed.
    """
    launched = asyncio.Future[bool]()
    if not _accepting:
        launched.set_result(False)
        return launched
    generation = _generation

    def launch() -> None:
        if generation != _generation or free_schedule_capacity() == 0:
            launched.set_result(False)
            return
        task = asyncio.create_task(factory(), name="schedule-execution", context=Context())
        _tasks.add(task)
        task.add_done_callback(_completed, context=Context())
        launched.set_result(True)

    run_after_commit(launch, on_rollback=partial(launched.set_result, False))
    return launched


async def stop_schedule_tasks() -> None:
    """Close admission before cancelling and awaiting every tracked execution."""
    global _accepting
    _accepting = False
    tasks = tuple(_tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.difference_update(tasks)
