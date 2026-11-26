"""The storage thread lane: the one place blocking S3 work runs.

Storage I/O must not queue behind (or in front of) the event loop's default executor, which `asyncio.to_thread` and
`anyio.to_thread` share with everything else. Every `S3Connection` owns a `ThreadLane` as `self.lane`, sized
`min(S3_THREAD_LANE_SIZE, S3_MAX_POOL_CONNECTIONS)` so no more threads run than the boto pool has connections.

API for storage code, including code added later (streamed writes, batch deletes, server-side copies, bounded reads):

- Route every blocking provider call, and every blocking step of a storage operation (hashing, opening an upload
  source), through `await self.lane.run(partial(client.operation, ...))`. Never call `asyncio.to_thread`,
  `anyio.to_thread` or `loop.run_in_executor` from storage code; `tests/test_storage_thread_lanes.py` fails if a
  storage call reaches either.
- `run` takes a no-argument callable and returns its result. Waiting for a free slot is cancellable and holds nothing.
- Default (`abandon_on_cancel=False`), same as `anyio.to_thread.run_sync`: an `anyio` cancel scope cannot interrupt
  the wait, so the call returns its result and the cancellation lands at the caller's next checkpoint. Use it for writes
  and deletes.
- `abandon_on_cancel=True`: a cancelled caller returns at once and the thread keeps running. Use it only when the
  worker owns and releases everything it opens (a GET that reads and closes its own body), because nobody awaits it.
- The lane slot belongs to the thread, not the caller: an abandoned call keeps its slot until the thread returns, so
  abandoned work can never push the lane past its size.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import anyio

from app.core.thread_slots import ThreadSlots


class ThreadLane:
    """A bounded set of dedicated threads; at most `size` calls run at once, the rest wait for a slot."""

    def __init__(self, size: int, name: str = "storage-lane") -> None:
        self.size: int = size
        self._slots: ThreadSlots = ThreadSlots(size)
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=size, thread_name_prefix=name)

    async def run[T](self, work: Callable[[], T], *, abandon_on_cancel: bool = False) -> T:
        await self._slots.acquire_async()
        try:
            running = self._executor.submit(contextvars.copy_context().run, work)
        except BaseException:
            self._slots.release()
            raise
        # The slot returns when the thread does, or when a call that never started is cancelled.
        running.add_done_callback(lambda _: self._slots.release())
        future = asyncio.wrap_future(running)
        if abandon_on_cancel:
            return await future
        with anyio.CancelScope(shield=True):
            _ = await asyncio.wait((future,))
        return future.result()
