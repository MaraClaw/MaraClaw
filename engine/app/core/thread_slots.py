"""A counted slot gate that threads and event-loop tasks wait on without polling.

Threads call `acquire` (the `threading.BoundedSemaphore` signature). Tasks `await acquire_async()`: the task parks on a
future and is resumed by `release`, so no waiter thread and no timer loop exists. Slots are handed to async waiters in
arrival order, and the holder that released is never the one that wakes them, so a slot is only ever owned by code that
is running.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from threading import Condition


@dataclass(eq=False)
class _AsyncWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    granted: bool = False

    def grant(self) -> bool:
        """Wake the task; `False` when its loop is gone and the slot must go to someone else."""
        try:
            self.loop.call_soon_threadsafe(_resolve, self.future)
        except RuntimeError:
            return False
        self.granted = True
        return True


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class ThreadSlots:
    """`size` slots shared by threads and tasks; a slot is returned with `release` by whoever holds it."""

    def __init__(self, size: int) -> None:
        if size < 1:
            raise ValueError("A slot gate needs at least one slot")
        self.size: int = size
        self._in_use: int = 0
        self._condition: Condition = Condition()
        self._waiters: deque[_AsyncWaiter] = deque()

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        """Take a slot from a thread; `False` when none was free (immediately, or after `timeout` seconds)."""
        with self._condition:
            if not blocking:
                return self._take_locked()
            if not self._condition.wait_for(lambda: self._in_use < self.size, timeout):
                return False
            return self._take_locked()

    async def acquire_async(self) -> None:
        """Take a slot from a task. Cancelled while waiting, the task leaves the queue and holds nothing."""
        waiter: _AsyncWaiter
        with self._condition:
            if self._take_locked():
                return
            loop = asyncio.get_running_loop()
            waiter = _AsyncWaiter(loop, loop.create_future())
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            with self._condition:
                if waiter.granted:
                    self._release_locked()
                else:
                    self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        with self._condition:
            self._release_locked()

    def _take_locked(self) -> bool:
        if self._in_use >= self.size:
            return False
        self._in_use += 1
        return True

    def _release_locked(self) -> None:
        if self._in_use == 0:
            raise ValueError("Slot released too many times")
        self._in_use -= 1
        while self._in_use < self.size and self._waiters:
            if self._waiters.popleft().grant():
                self._in_use += 1
        if self._in_use < self.size:
            self._condition.notify_all()
