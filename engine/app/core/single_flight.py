"""Per-key single-flight: concurrent callers of one key share a single load.

The load runs as its own task, so cancelling a waiter (a closed request, a timeout) never cancels it for the others.
A failed load reaches every waiter and is forgotten, so the next caller starts a fresh one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import final


@final
class SingleFlight[K: Hashable, T]:
    def __init__(self) -> None:
        self._loads: dict[K, asyncio.Task[T]] = {}

    async def run(self, key: K, load: Callable[[], Awaitable[T]]) -> T:
        task = self._loads.get(key)
        if task is None:
            task = asyncio.create_task(self._load(load))
            self._loads[key] = task
            task.add_done_callback(lambda done: self._forget(key, done))
        return await asyncio.shield(task)

    @staticmethod
    async def _load(load: Callable[[], Awaitable[T]]) -> T:
        return await load()

    def _forget(self, key: K, done: asyncio.Task[T]) -> None:
        if self._loads.get(key) is done:
            del self._loads[key]
        if not done.cancelled():
            # Every waiter may have been cancelled; a failure nobody awaits is not an unretrieved task exception.
            _ = done.exception()
