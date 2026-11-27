"""Loop-owned, prioritized and bounded threads for blocking guest Docker CLI calls.

Guest start, stop, admission, readiness, container-status calls and the background sweeps run here instead of on
anyio's default thread limiter, so a stalled Docker daemon parks only this lane's threads and unrelated ``to_thread``
work keeps its tokens. The long-lived Docker events reader and native turns (``openclaw_native_capacity``) stay off
this lane.

Calls beyond the lane's threads wait in one queue per priority and are admitted interactive first, each class in
arrival order. A class whose queue is full refuses the next call with ``DockerBusyError``: HTTP boundaries answer
503 with ``Retry-After``, and background loops skip the pass. Background code reaches the lane at background priority
through ``background_docker()`` (or ``priority=`` on one call).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from contextvars import ContextVar, copy_context
from enum import IntEnum
from typing import ClassVar
from weakref import WeakKeyDictionary

from anyio import CancelScope

from app.config import get_settings


class DockerPriority(IntEnum):
    """Lower values are admitted first."""

    INTERACTIVE = 0
    BACKGROUND = 1


class DockerBusyError(Exception):
    """The lane's queue for this priority is full; retry shortly. HTTP boundaries answer 503 with ``Retry-After``."""

    retry_after_seconds: ClassVar[int] = 5

    def __init__(self) -> None:
        super().__init__("Docker is busy; retry shortly")


docker_priority: ContextVar[DockerPriority] = ContextVar("docker_priority", default=DockerPriority.INTERACTIVE)


@contextmanager
def background_docker() -> Generator[None]:
    """Run the Docker calls of this context, nested service calls included, at background priority."""
    token = docker_priority.set(DockerPriority.BACKGROUND)
    try:
        yield
    finally:
        docker_priority.reset(token)


def _discard[T](waiter: asyncio.Future[T]) -> None:
    """Mark the outcome of a call that nobody awaits any more as retrieved."""
    if not waiter.cancelled():
        _ = waiter.exception()


class DockerCapacity:
    """A lazy thread pool for one loop; a slot is one worker thread, busy until its call returns.

    Slots are counted on the loop, so waiting callers sit in the priority queues rather than in the executor, and a
    caller cancelled while queued simply leaves.
    """

    def __init__(self, limit: int | None = None, queue_limit: int | None = None) -> None:
        settings = get_settings()
        self.limit: int = settings.DOCKER_LANE_THREADS if limit is None else limit
        self.queue_limit: int = settings.DOCKER_LANE_QUEUE_LIMIT if queue_limit is None else queue_limit
        self.executor: ThreadPoolExecutor | None = None
        self._running: int = 0
        self._queues: dict[DockerPriority, deque[asyncio.Future[None]]] = {
            priority: deque() for priority in DockerPriority
        }

    async def _admit(self, priority: DockerPriority) -> None:
        """Take a slot now, or wait in this priority's queue; raise ``DockerBusyError`` when that queue is full."""
        if self._running < self.limit:
            self._running += 1
            return
        queue = self._queues[priority]
        if len(queue) >= self.queue_limit:
            raise DockerBusyError
        grant = asyncio.get_running_loop().create_future()
        queue.append(grant)
        try:
            await grant
        except asyncio.CancelledError:
            if grant.cancelled():
                queue.remove(grant)
            else:
                # The slot was handed over in the same loop iteration as the cancellation; pass it on.
                self._release()
            raise

    def _release(self) -> None:
        self._running -= 1
        for priority in DockerPriority:
            queue = self._queues[priority]
            while queue and self._running < self.limit:
                grant = queue.popleft()
                if not grant.done():
                    self._running += 1
                    grant.set_result(None)

    async def run[*Ts, R](
        self,
        function: Callable[[*Ts], R],
        *args: *Ts,
        priority: DockerPriority | None = None,
        abandon_on_cancel: bool = False,
    ) -> R:
        """Match ``to_thread.run_sync``'s context propagation and cancellation without borrowing default threads.

        ``priority`` defaults to the context's (``background_docker()``), else interactive. A cancelled call that has
        not started never runs, whether it waits in the queue or has not yet been picked up by a thread. One that is
        running keeps its slot until its thread returns: the caller waits for it, shielded, unless
        ``abandon_on_cancel`` lets the caller return at once.
        """
        await self._admit(docker_priority.get() if priority is None else priority)
        try:
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="docker-lane")
            context = copy_context()
            future = self.executor.submit(lambda: context.run(function, *args))
        except BaseException:
            self._release()
            raise
        waiter = asyncio.wrap_future(future)
        waiter.add_done_callback(lambda _: self._release())
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
        for queue in self._queues.values():
            for grant in queue:
                _ = grant.cancel()
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


async def run_docker[*Ts, R](
    function: Callable[[*Ts], R],
    *args: *Ts,
    priority: DockerPriority | None = None,
    abandon_on_cancel: bool = False,
) -> R:
    """Run one blocking Docker call on this loop's lane; a slot is taken per call, never across a sequence."""
    return await docker_capacity().run(function, *args, priority=priority, abandon_on_cancel=abandon_on_cancel)


def shutdown_docker_capacity() -> None:
    """Called once the work that issues Docker calls has drained; never waits for a hung CLI."""
    capacity = _capacities.pop(asyncio.get_running_loop(), None)
    if capacity is not None:
        capacity.shutdown()
