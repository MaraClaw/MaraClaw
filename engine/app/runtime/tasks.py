"""Owned asyncio tasks for SDK compatibility; never discover global loop tasks."""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Literal, TypeIs

from app.core.logging import logger


async def cancel_tasks(tasks: Iterable[asyncio.Task[object]]) -> None:
    owned = tuple(tasks)
    for task in owned:
        if not task.done():
            _ = task.cancel()
    results = await asyncio.gather(*owned, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            logger.warning("[shutdown] Owned task failed: {}", result)


class OwnedTasks:
    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[object]] = set()
        self.required: dict[str, tuple[asyncio.Task[object], bool]] = {}

    def start(
        self,
        name: str,
        coroutine: Coroutine[object, object, object],
        *,
        requirement: Literal["optional", "setup", "loop"] = "optional",
    ) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        if requirement != "optional":
            self.required[name] = (task, requirement == "setup")
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self._report)

    def ready(self) -> bool:
        for task, finite in self.required.values():
            if finite:
                if not task.done() or task.cancelled() or task.exception() is not None:
                    return False
            elif task.done():
                return False
        return True

    @staticmethod
    def _report(task: asyncio.Task[object]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("[runtime] Background task {} failed: {}", task.get_name(), error)

    async def stop(self) -> None:
        await cancel_tasks(self.tasks)
        self.tasks.clear()


class EventTasks:
    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[object]] = set()
        self.closing: bool = False

    def wrap[**P, R](self, callback: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[object, object, R]]:
        async def owned(*args: P.args, **kwargs: P.kwargs) -> R:
            if self.closing:
                raise asyncio.CancelledError
            task = asyncio.current_task()
            if task:
                self.tasks.add(task)
            try:
                return await callback(*args, **kwargs)
            finally:
                if task:
                    self.tasks.discard(task)

        return owned

    async def stop(self) -> None:
        self.closing = True
        await cancel_tasks(self.tasks)
        self.tasks.clear()


def is_async_callback(value: object) -> TypeIs[Callable[..., Awaitable[object]]]:
    return callable(value) and inspect.iscoroutinefunction(value)
