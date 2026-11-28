"""A bounded set of dedicated threads for blocking work, apart from the event loop's default executor.

`asyncio.to_thread` and `anyio.to_thread` share one pool with everything else in the process. A `ThreadLane` owns its
threads, so slow work in one subsystem (S3 calls, document conversion) never queues behind or in front of another's.

- `run` takes a no-argument callable and returns its result. Waiting for a free slot is cancellable and holds nothing.
- Default (`abandon_on_cancel=False`), same as `anyio.to_thread.run_sync`: an `anyio` cancel scope cannot interrupt
  the wait, so the call returns its result and the cancellation lands at the caller's next checkpoint. Use it for writes
  and deletes.
- `abandon_on_cancel=True`: a cancelled caller returns at once and the thread keeps running. Use it only when the
  worker owns and releases everything it opens (a GET that reads and closes its own body), or when the caller must be
  able to leave at a deadline (a conversion), because nobody awaits it.
- The lane slot belongs to the thread, not the caller: an abandoned call keeps its slot until the thread returns, so
  abandoned work can never push the lane past its size.
- `max_waiters` (default `None`: unbounded) bounds the calls waiting for a slot. A `run` that finds every slot taken
  and the queue full raises `SlotsBusyError` before it starts anything and holds nothing. Only the S3 lane sets it.

Users: `S3Connection.lane` (`app/services/storage_runtime/AGENTS.md`), the document conversion lane
(`app/services/document_conversion/conversion_lane.py`), the agent email lane (`email_service.AGENT_EMAIL_LANE`) and the
system email lane (`system_email_service.SYSTEM_EMAIL_LANE`; `app/services/AGENTS.md`).
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

    def __init__(self, size: int, name: str = "thread-lane", *, max_waiters: int | None = None) -> None:
        self.size: int = size
        self._slots: ThreadSlots = ThreadSlots(size, max_waiters=max_waiters)
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
