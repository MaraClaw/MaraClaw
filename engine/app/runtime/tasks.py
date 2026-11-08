"""Owned asyncio tasks for SDK compatibility; never discover global loop tasks."""

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
from time import monotonic
from typing import Literal, TypeIs

from app.core.incident_reporting import report_exception
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


class AdmissionClosedError(Exception):
    """Work was refused by a closed gate, or cancelled by a drain that ran out of grace."""


class TaskAdmission:
    """Admission gate and bounded drain for module-owned work.

    Detached tasks live in ``tasks`` (the module's own set). Work that must not be
    refused halfway (a claim followed by its launch, a wake the caller awaits) runs
    through ``run``: admission is checked once, synchronously, then the work is an
    owned child task the caller awaits. ``drain`` closes the gate, waits for that work
    and the tasks within one grace period, then cancels and awaits what remains. Work
    is joined before its tasks are snapshotted, so nothing can start after a drain.
    """

    def __init__(self, label: str, tasks: set[asyncio.Task[None]]) -> None:
        self.label: str = label
        self.tasks: set[asyncio.Task[None]] = tasks
        self.accepting: bool = True
        self._operations: set[asyncio.Task[object]] = set()

    def open(self) -> None:
        self.accepting = True

    def close(self) -> None:
        self.accepting = False

    async def run[R](self, start: Callable[[], Coroutine[object, object, R]]) -> R:
        """Run admitted work as an owned child; raise ``AdmissionClosedError`` if refused or drained."""
        if not self.accepting:
            raise AdmissionClosedError(self.label)
        operation = asyncio.create_task(start())
        self._operations.add(operation)
        operation.add_done_callback(self._operations.discard)
        try:
            return await operation
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if operation.cancelled() and caller is not None and caller.cancelling() == 0:
                raise AdmissionClosedError(self.label) from None
            raise

    async def drain(self, grace_seconds: float = 20.0) -> None:
        self.close()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_seconds
        await self._join(tuple(self._operations), deadline - loop.time())
        tasks = tuple(self.tasks)
        await self._join(tasks, deadline - loop.time())
        self.tasks.difference_update(tasks)

    async def _join[T: object](self, owned: tuple[asyncio.Task[T], ...], grace_seconds: float) -> None:
        if not owned:
            return
        _, pending = await asyncio.wait(owned, timeout=max(0.0, grace_seconds))
        if pending:
            logger.warning("[shutdown] {} drain timed out; cancelling {} owned tasks", self.label, len(pending))
            await cancel_tasks(pending)


class RequiredLoopExitedError(RuntimeError):
    """A required ongoing task returned before runtime shutdown."""


def capped_backoff(failures: int, *, base_seconds: float, cap_seconds: float) -> float:
    """Delay after ``failures`` consecutive failures: ``base`` doubling up to ``cap``."""
    return min(cap_seconds, base_seconds * (1 << min(failures, 32)))


class LoopProgress:
    """Last-beat record of a required loop that opted into a progress budget.

    The loop beats when it finishes a pass. It is stale once the last beat is older than
    its budget on the owner's injectable monotonic clock, except while inside ``exempt``.
    """

    def __init__(self, budget_seconds: float, clock: Callable[[], float]) -> None:
        self.budget_seconds: float = budget_seconds
        self._clock: Callable[[], float] = clock
        self._last_beat: float = clock()
        self._exempt: int = 0

    def beat(self) -> None:
        self._last_beat = self._clock()

    @contextmanager
    def exempt(self, *, restart_on_error: bool = True) -> Generator[None]:
        """Exclude work with its own duration contract; optionally retain failure evidence."""
        self._exempt += 1
        try:
            yield
        except BaseException:
            if restart_on_error:
                self.beat()
            raise
        else:
            self.beat()
        finally:
            self._exempt -= 1

    def stale(self) -> bool:
        return self._exempt == 0 and self._clock() - self._last_beat > self.budget_seconds


_loop_progress: ContextVar[LoopProgress | None] = ContextVar("loop_progress", default=None)


def loop_progress() -> LoopProgress:
    """Progress record of the budgeted loop running this task; inert (never stale) for any other caller."""
    return _loop_progress.get() or LoopProgress(math.inf, monotonic)


class OwnedTasks:
    def __init__(self, clock: Callable[[], float] = monotonic) -> None:
        self.tasks: set[asyncio.Task[object]] = set()
        self.required: dict[str, tuple[asyncio.Task[object], bool]] = {}
        self.progress: dict[str, LoopProgress] = {}
        self.closing: bool = False
        self._clock: Callable[[], float] = clock

    def start(
        self,
        name: str,
        coroutine: Coroutine[object, object, object],
        *,
        requirement: Literal["optional", "setup", "loop"] = "optional",
        progress_budget_seconds: float | None = None,
    ) -> None:
        """Own ``coroutine``; a required loop with a budget must call ``loop_progress().beat()`` once per pass."""
        if progress_budget_seconds is None:
            task = asyncio.create_task(coroutine, name=name)
        else:
            if requirement != "loop":
                raise ValueError("a progress budget applies only to required loops")
            progress = self.progress[name] = LoopProgress(progress_budget_seconds, self._clock)
            context = copy_context()
            _ = context.run(_loop_progress.set, progress)
            task = asyncio.create_task(coroutine, name=name, context=context)
        self.tasks.add(task)
        if requirement != "optional":
            self.required[name] = (task, requirement == "setup")
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self._report)

    def ready(self) -> bool:
        for name, (task, finite) in self.required.items():
            if finite:
                if not task.done() or task.cancelled() or task.exception() is not None:
                    return False
            elif task.done() or ((progress := self.progress.get(name)) is not None and progress.stale()):
                return False
        return True

    def _report(self, task: asyncio.Task[object]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            report_exception(error, source="runtime", operation="owned-task")
            logger.error("[runtime] Background task {} failed: {}", task.get_name(), error)
        elif (
            not task.cancelled()
            and not self.closing
            and any(required is task and not finite for required, finite in self.required.values())
        ):
            report_exception(RequiredLoopExitedError(), source="runtime", operation="required-loop-exited")

    async def stop(self) -> None:
        self.closing = True
        await cancel_tasks(self.tasks)
        self.tasks.clear()


class EventTasks:
    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[object]] = set()
        self.closing: bool = False

    def wrap[**P, R](self, callback: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[object, object, R]]:
        @wraps(callback)
        async def owned(*args: P.args, **kwargs: P.kwargs) -> R:
            if self.closing:
                raise asyncio.CancelledError
            task = asyncio.current_task()
            outermost = task not in self.tasks
            if task:
                self.tasks.add(task)
            try:
                return await callback(*args, **kwargs)
            finally:
                if task and outermost:
                    self.tasks.discard(task)

        return owned

    async def stop(self) -> None:
        self.closing = True
        await cancel_tasks(self.tasks)
        self.tasks.clear()


def is_async_callback(value: object) -> TypeIs[Callable[..., Awaitable[object]]]:
    return callable(value) and inspect.iscoroutinefunction(value)
