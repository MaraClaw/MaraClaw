"""Finite business admission for SDK callbacks, separate from transport ownership."""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future, InvalidStateError
from typing import Final, final, override

from app.runtime.tasks import EventTasks

BUSINESS_CAPACITY: Final = 8


class CallbackRefusedError(RuntimeError):
    """A client's business capacity is full or its lifecycle has closed."""


@final
class CallbackAdmission(EventTasks):
    """Per-client ownership, eight business slots, zero capacity waiters.

    Pending SDK-thread submissions consume slots. Cancellation releases a slot only
    after the application task finishes its cleanup. Transport uses ``wrap`` without
    a slot. One separate best-effort refusal reply may run, with zero reply waiters.
    Closed scopes stay closed; a new client requires a fresh scope.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._active: int = 0
        self._reporting: bool = False

    def _claim(self) -> None:
        if self.closing:
            raise CallbackRefusedError("Connector message admission is closed")
        if self._active >= BUSINESS_CAPACITY:
            raise CallbackRefusedError("Connector message capacity is full")
        self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    async def run[**P, R](self, callback: Callable[P, Awaitable[R]], *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            self._claim()
        try:
            return await self.wrap(callback)(*args, **kwargs)
        finally:
            self._release()

    async def report_refusal[**P](
        self, callback: Callable[P, Awaitable[object]], *args: P.args, **kwargs: P.kwargs
    ) -> None:
        with self._lock:
            if self.closing or self._reporting:
                return
            self._reporting = True
        try:
            _ = await self.wrap(callback)(*args, **kwargs)
        finally:
            with self._lock:
                self._reporting = False

    def submit[R](self, loop: asyncio.AbstractEventLoop, callback: Callable[[], Awaitable[R]]) -> Future[R]:
        """Reserve and post atomically with closure; never construct a coroutine in the SDK thread."""
        result = Future[R]()

        def launch() -> None:
            if result.cancelled() or self.closing:
                self._release()
                try:
                    result.set_exception(CallbackRefusedError("Connector message admission is closed"))
                except InvalidStateError:
                    if not result.cancelled():
                        raise
                return
            task = asyncio.create_task(self.wrap(callback)())
            self.tasks.add(task)

            def finished(done: asyncio.Task[R]) -> None:
                self.tasks.discard(done)
                self._release()
                error = None if done.cancelled() else done.exception()
                try:
                    if done.cancelled():
                        _ = result.cancel()
                    elif error is not None:
                        result.set_exception(error)
                    else:
                        result.set_result(done.result())
                except InvalidStateError:
                    if not result.cancelled():
                        raise

            def cancelled(completion: Future[R]) -> None:
                if completion.cancelled() and not task.done():
                    _ = loop.call_soon_threadsafe(task.cancel)

            task.add_done_callback(finished)
            result.add_done_callback(cancelled)

        with self._lock:
            self._claim()
            try:
                _ = loop.call_soon_threadsafe(launch)
            except RuntimeError:
                self._active -= 1
                raise
        return result

    def close(self) -> None:
        with self._lock:
            self.closing = True

    @override
    async def stop(self) -> None:
        self.close()
        loop = asyncio.get_running_loop()
        submitted = loop.create_future()
        _ = loop.call_soon(submitted.set_result, None)
        await submitted
        await super().stop()
