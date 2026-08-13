"""Ownership for route-launched background tasks."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.core.logging import logger

_tasks: set[asyncio.Task[object]] = set()


def schedule_background_task(
    coroutine: Coroutine[Any, Any, object],
    description: str,
) -> asyncio.Task[object]:
    """Schedule work while retaining and observing the task until it completes."""
    task = asyncio.create_task(coroutine)
    _tasks.add(task)

    def observe_completion(completed_task: asyncio.Task[object]) -> None:
        _tasks.discard(completed_task)
        if completed_task.cancelled():
            logger.debug(f"Background task cancelled: {description}")
            return
        if error := completed_task.exception():
            logger.opt(exception=error).error(f"Background task failed: {description}")

    task.add_done_callback(observe_completion)
    return task
