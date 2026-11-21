"""Loop-owned threads for blocking guest Docker CLI calls.

Guest start, stop, admission, readiness and container-status calls run here instead of on anyio's default thread
limiter, so a stalled Docker daemon parks only this lane's threads and unrelated ``to_thread`` work keeps its tokens.
The long-lived Docker events reader and native turns (``openclaw_native_capacity``) stay off this lane.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextvars import copy_context
from weakref import WeakKeyDictionary

from anyio import CancelScope

# Docker CLI calls that run at once per event loop; further calls queue in order for a free thread.
DOCKER_LANE_THREADS = 16


def _discard[T](waiter: asyncio.Future[T]) -> None:
    """Mark the outcome of a call that nobody awaits any more as retrieved."""
    if not waiter.cancelled():
        _ = waiter.exception()


class DockerCapacity:
    """A lazy thread pool for one loop; a slot is one worker thread, busy until its call returns."""

    def __init__(self) -> None:
        self.limit: int = DOCKER_LANE_THREADS
        self.executor: ThreadPoolExecutor | None = None

    async def run[*Ts, R](self, function: Callable[[*Ts], R], *args: *Ts, abandon_on_cancel: bool = False) -> R:
        """Match ``to_thread.run_sync``'s context propagation and cancellation without borrowing default threads.

        A cancelled call that has not started never runs. One that is running keeps its slot until its thread returns:
        the caller waits for it, shielded, unless ``abandon_on_cancel`` lets the caller return at once.
        """
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="docker-lane")
        context = copy_context()
        future = self.executor.submit(lambda: context.run(function, *args))
        waiter = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            waiter.add_done_callback(_discard)
            if not future.cancel() and not abandon_on_cancel:
                with CancelScope(shield=True):
                    while not waiter.done():
                        with suppress(asyncio.CancelledError):
                            _ = await asyncio.wait((waiter,))
            raise

    def shutdown(self) -> None:
        """Drop queued calls and return without joining running ones; their CLI deadlines bound them."""
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = None


_capacities: WeakKeyDictionary[asyncio.AbstractEventLoop, DockerCapacity] = WeakKeyDictionary()


def docker_capacity() -> DockerCapacity:
    loop = asyncio.get_running_loop()
    capacity = _capacities.get(loop)
    if capacity is None:
        capacity = DockerCapacity()
        _capacities[loop] = capacity
    return capacity


async def run_docker[*Ts, R](function: Callable[[*Ts], R], *args: *Ts, abandon_on_cancel: bool = False) -> R:
    """Run one blocking Docker call on this loop's lane; a slot is taken per call, never across a sequence."""
    return await docker_capacity().run(function, *args, abandon_on_cancel=abandon_on_cancel)


def shutdown_docker_capacity() -> None:
    """Called once the work that issues Docker calls has drained; never waits for a hung CLI."""
    capacity = _capacities.pop(asyncio.get_running_loop(), None)
    if capacity is not None:
        capacity.shutdown()
