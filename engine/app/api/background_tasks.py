"""Launch work after commit in a fresh trace-only context; never on rollback."""

import asyncio
import contextvars
from collections.abc import Coroutine
from typing import Any

from app.core.logging import get_trace_id, logger, set_trace_id
from app.db.session import run_after_commit

_tasks: set[asyncio.Task[object]] = set()


def schedule_background_task(
    coroutine: Coroutine[Any, Any, object],
    description: str,
) -> None:
    """Start after the caller commits, in a fresh trace-only context; drop on rollback."""
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
            logger.opt(exception=error).error(f"Background task failed: {description}")

    def launch() -> None:
        task = asyncio.create_task(coroutine, context=ctx)
        _tasks.add(task)
        task.add_done_callback(observe_completion, context=ctx)

    def drop() -> None:
        coroutine.close()
        logger.warning(f"Background task dropped because transaction rolled back: {description}")

    run_after_commit(launch, drop)
