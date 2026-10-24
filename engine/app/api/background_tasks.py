"""Launch work after commit in a fresh trace-only context; never on rollback."""

import asyncio
import contextvars
from collections.abc import Coroutine
from typing import Any

from app.core.incident_reporting import report_exception
from app.core.logging import get_trace_id, logger, set_trace_id
from app.db.session import run_after_commit

_tasks: set[asyncio.Task[object]] = set()
_accepting = True
_generation = 0


def start_background_tasks() -> None:
    """Open admission for a new lifespan, invalidating older deferred launches."""
    global _accepting, _generation
    _generation += 1
    _accepting = True


async def drain_background_tasks(grace_seconds: float = 20.0) -> None:
    """Close admission, allow a bounded grace period, then cancel and join owned work."""
    global _accepting
    _accepting = False
    tasks = tuple(_tasks)
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
    if pending:
        logger.warning("Background task drain timed out; cancelling {} owned tasks", len(pending))
        for task in pending:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.difference_update(tasks)


def schedule_background_task(
    coroutine: Coroutine[Any, Any, object],
    description: str,
) -> None:
    """Start after the caller commits, in a fresh trace-only context; drop on rollback."""

    def reject() -> None:
        coroutine.close()
        logger.warning("Background task rejected during shutdown or from an older lifespan: {}", description)

    if not _accepting:
        reject()
        return
    generation = _generation
    ctx = contextvars.Context()
    trace_id = get_trace_id()
    if trace_id is not None:
        ctx.run(set_trace_id, trace_id)

    def observe_completion(completed_task: asyncio.Task[object]) -> None:
        _tasks.discard(completed_task)
        if completed_task.cancelled():
            logger.debug(f"Background task cancelled: {description}")
            return
        if error := completed_task.exception():
            report_exception(error, source="background", operation="route-task")
            logger.opt(exception=error).error(f"Background task failed: {description}")

    def launch() -> None:
        if not _accepting or generation != _generation:
            reject()
            return
        task = asyncio.create_task(coroutine, context=ctx)
        _tasks.add(task)
        task.add_done_callback(observe_completion, context=ctx)

    def drop() -> None:
        coroutine.close()
        logger.warning(f"Background task dropped because transaction rolled back: {description}")

    run_after_commit(launch, drop)
