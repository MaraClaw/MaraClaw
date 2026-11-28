"""Owned asyncio tasks for SDK compatibility; never discover global loop tasks."""

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import wraps
from random import random as system_random
from time import monotonic
from typing import Literal, TypeIs

from app.core.incident_reporting import report_exception
from app.core.logging import logger
from app.runtime.shutdown_budget import join


async def cancel_tasks(tasks: Iterable[asyncio.Task[object]], *, label: str = "Owned") -> None:
    """Cancel and join ``tasks``; inside a shutdown step the join stops at the step's deadline and names the rest."""
    owned = tuple(tasks)
    for task in owned:
        if not task.done():
            _ = task.cancel()
    await join(owned, f"{label} tasks")
    for task in owned:
        if task.done() and not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("[shutdown] Owned task failed: {}", error)


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
            await cancel_tasks(pending, label=self.label)


class RequiredLoopExitedError(RuntimeError):
    """A required ongoing task returned before runtime shutdown."""


RESTART_BASE_SECONDS = 1.0


def capped_backoff(failures: int, *, base_seconds: float, cap_seconds: float) -> float:
    """Delay after ``failures`` consecutive failures: ``base`` doubling up to ``cap``."""
    return min(cap_seconds, base_seconds * (1 << min(failures, 32)))


def jittered_backoff(
    failures: int, *, base_seconds: float, cap_seconds: float, random: Callable[[], float] = system_random
) -> float:
    """Delay after ``failures`` consecutive failures, drawn from the upper half of the next doubling step.

    The window is ``[max(base, step / 2), step]`` for ``step = capped_backoff(failures + 1)``: even the first retry is
    spread, the floor never drops as failures grow, and the result stays within ``[base, cap]`` for any ``random``
    (its value is clamped to ``[0, 1]``). Pure: no sleeping, no I/O.
    """
    if base_seconds <= 0 or cap_seconds < base_seconds:
        raise ValueError("base_seconds must be positive and cap_seconds at least base_seconds")
    step = capped_backoff(failures + 1, base_seconds=base_seconds, cap_seconds=cap_seconds)
    low = max(base_seconds, step / 2)
    return low + min(max(random(), 0.0), 1.0) * (step - low)


@dataclass(slots=True)
class ReconnectBackoff:
    """Jittered reconnect delays that escalate until a run stays healthy.

    A run that lasted ``healthy_seconds`` on the monotonic ``clock`` proved the connection worked, so the next
    failure starts over at the first delay; anything shorter, including a run that ended the moment it connected,
    keeps escalating. Read ``started()`` before each attempt and pass it to ``next_delay`` when the attempt ends.
    """

    base_seconds: float
    cap_seconds: float
    healthy_seconds: float
    random: Callable[[], float] = system_random
    clock: Callable[[], float] = monotonic
    failures: int = 0

    def started(self) -> float:
        return self.clock()

    def settle(self, started_at: float) -> None:
        """Start over if the run that began at ``started_at`` has lasted ``healthy_seconds``."""
        if self.clock() - started_at >= self.healthy_seconds:
            self.failures = 0

    def next_delay(self, started_at: float) -> float:
        """Delay before the next attempt, after the attempt that began at ``started_at`` ended."""
        self.settle(started_at)
        delay = jittered_backoff(
            self.failures, base_seconds=self.base_seconds, cap_seconds=self.cap_seconds, random=self.random
        )
        self.failures += 1
        return delay


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
        self._awaiting_beat: bool = False

    def beat(self) -> None:
        self._last_beat = self._clock()
        self._awaiting_beat = False

    def restarted(self) -> None:
        """A new incarnation began: the start counts as a beat, but only a real ``beat`` makes the loop ready."""
        self._last_beat = self._clock()
        self._awaiting_beat = True

    def awaiting_beat(self) -> bool:
        return self._awaiting_beat

    @contextmanager
    def exempt(self, *, restart_on_error: bool = True) -> Generator[None]:
        """Exclude work with its own duration contract; optionally retain failure evidence."""
        self._exempt += 1
        try:
            yield
        except BaseException:
            if restart_on_error:
                self._last_beat = self._clock()
            raise
        else:
            self._last_beat = self._clock()
        finally:
            self._exempt -= 1

    def seconds_since_beat(self) -> float:
        return self._clock() - self._last_beat

    def stale(self) -> bool:
        return self._exempt == 0 and self.seconds_since_beat() > self.budget_seconds


_loop_progress: ContextVar[LoopProgress | None] = ContextVar("loop_progress", default=None)


def loop_progress() -> LoopProgress:
    """Progress record of the budgeted loop running this task; inert (never stale) for any other caller."""
    return _loop_progress.get() or LoopProgress(math.inf, monotonic)


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """How a supervised required loop is restarted.

    ``budget`` is the number of restarts the loop may use over the life of the process; the next exit is final.
    Delays double from ``RESTART_BASE_SECONDS`` up to ``max_backoff_seconds``. ``before_restart`` runs after the
    delay and before the new incarnation starts, so work the dead one left behind can be settled first.
    """

    budget: int
    max_backoff_seconds: float
    before_restart: Callable[[], Awaitable[None]] | None = None


type LoopRedReason = Literal["stale", "ended", "exhausted"]
"""Why a required loop is not ready: no beat within its budget, its task or incarnation ended, or it is down for good."""


@dataclass(frozen=True, slots=True)
class LoopSnapshot:
    """Read-only state of one required ongoing loop as readiness, liveness and the metrics scrape see it."""

    name: str
    reason: LoopRedReason | None
    stale: bool
    seconds_since_beat: float | None
    restarts: int
    exhausted: bool

    @property
    def ready(self) -> bool:
        return self.reason is None


class OwnedTasks:
    def __init__(
        self,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.tasks: set[asyncio.Task[object]] = set()
        self.required: dict[str, tuple[asyncio.Task[object], bool]] = {}
        self.progress: dict[str, LoopProgress] = {}
        self.restarts: dict[str, int] = {}
        self.exhausted: set[str] = set()
        self.closing: bool = False
        self._clock: Callable[[], float] = clock
        self._sleep: Callable[[float], Awaitable[None]] = sleep

    def start(
        self,
        name: str,
        coroutine: Coroutine[object, object, object],
        *,
        requirement: Literal["optional", "setup", "loop"] = "optional",
        progress_budget_seconds: float | None = None,
    ) -> None:
        """Own ``coroutine``; a required loop with a budget must call ``loop_progress().beat()`` once per pass."""
        progress = None
        if progress_budget_seconds is not None:
            if requirement != "loop":
                raise ValueError("a progress budget applies only to required loops")
            progress = self.progress[name] = LoopProgress(progress_budget_seconds, self._clock)
        _ = self._launch(name, coroutine, requirement, progress)

    def start_supervised(
        self,
        name: str,
        factory: Callable[[], Coroutine[object, object, object]],
        *,
        progress_budget_seconds: float,
        policy: RestartPolicy,
    ) -> None:
        """Own a required budgeted loop and restart it from a supervisor task when it exits before shutdown.

        Every incarnation is a fresh coroutine from ``factory``, started only after the previous task finished.
        A restarted loop is unready until its first real beat; stale progress alone never restarts it.
        """
        progress = self.progress[name] = LoopProgress(progress_budget_seconds, self._clock)
        self.restarts[name] = 0
        _ = self._launch(name, factory(), "loop", progress)
        supervisor = asyncio.create_task(self._supervise(name, factory, progress, policy), name=f"{name}-supervisor")
        self.tasks.add(supervisor)
        supervisor.add_done_callback(self.tasks.discard)
        supervisor.add_done_callback(self._report)

    def _launch(
        self,
        name: str,
        coroutine: Coroutine[object, object, object],
        requirement: Literal["optional", "setup", "loop"],
        progress: LoopProgress | None,
    ) -> asyncio.Task[object]:
        if progress is None:
            task = asyncio.create_task(coroutine, name=name)
        else:
            context = copy_context()
            _ = context.run(_loop_progress.set, progress)
            task = asyncio.create_task(coroutine, name=name, context=context)
        self.tasks.add(task)
        if requirement != "optional":
            self.required[name] = (task, requirement == "setup")
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self._report)
        return task

    async def _supervise(
        self,
        name: str,
        factory: Callable[[], Coroutine[object, object, object]],
        progress: LoopProgress,
        policy: RestartPolicy,
    ) -> None:
        try:
            while True:
                incarnation = self.required[name][0]
                _ = await asyncio.wait((incarnation,))
                if self.closing:
                    return
                error = _exit_error(incarnation)
                used = self.restarts[name]
                if used >= policy.budget:
                    self.exhausted.add(name)
                    logger.error(
                        "[runtime] Required loop {} exhausted its {} restarts and stays down: {!r}",
                        name,
                        policy.budget,
                        error,
                    )
                    return
                delay = capped_backoff(used, base_seconds=RESTART_BASE_SECONDS, cap_seconds=policy.max_backoff_seconds)
                logger.warning(
                    "[runtime] Restarting required loop {} in {:g}s (restart {} of {}): {!r}",
                    name,
                    delay,
                    used + 1,
                    policy.budget,
                    error,
                )
                await self._sleep(delay)
                if policy.before_restart is not None:
                    await policy.before_restart()
                self.restarts[name] = used + 1
                progress.restarted()
                _ = self._launch(name, factory(), "loop", progress)
        except Exception:
            self.exhausted.add(name)
            raise

    def ready(self) -> bool:
        for name, (task, finite) in self.required.items():
            if finite:
                if not task.done() or task.cancelled() or task.exception() is not None:
                    return False
            elif self._loop_reason(name, task) is not None:
                return False
        return True

    def loop_snapshots(self) -> tuple[LoopSnapshot, ...]:
        """State of every required ongoing loop; reads in-process records only, never the pool, Redis or a lock."""
        snapshots: list[LoopSnapshot] = []
        for name, (task, finite) in self.required.items():
            if finite:
                continue
            progress = self.progress.get(name)
            snapshots.append(
                LoopSnapshot(
                    name=name,
                    reason=self._loop_reason(name, task),
                    stale=progress is not None and progress.stale(),
                    seconds_since_beat=None if progress is None else progress.seconds_since_beat(),
                    restarts=self.restarts.get(name, 0),
                    exhausted=name in self.exhausted,
                )
            )
        return tuple(snapshots)

    def _loop_reason(self, name: str, task: asyncio.Task[object]) -> LoopRedReason | None:
        """The one place that decides a required ongoing loop is not ready; `None` while it is."""
        progress = self.progress.get(name)
        if name in self.exhausted:
            return "exhausted"
        if task.done() or (progress is not None and progress.awaiting_beat()):
            return "ended"
        if progress is not None and progress.stale():
            return "stale"
        return None

    def _report(self, task: asyncio.Task[object]) -> None:
        loop = next((name for name, (owned, finite) in self.required.items() if owned is task and not finite), None)
        if not task.cancelled() and (error := task.exception()) is not None:
            report_exception(error, source="runtime", operation="owned-task")
            logger.error("[runtime] Background task {} failed: {}", task.get_name(), error)
        elif loop is not None and not task.cancelled() and not self.closing:
            report_exception(RequiredLoopExitedError(), source="runtime", operation="required-loop-exited")
        if loop is not None and not self.closing and loop not in self.restarts:
            # Nothing restarts an unsupervised required loop, so any exit before shutdown is final.
            self.exhausted.add(loop)

    async def stop(self) -> None:
        self.closing = True
        await cancel_tasks(self.tasks)
        self.tasks.clear()


def _exit_error(task: asyncio.Task[object]) -> BaseException:
    if task.cancelled():
        return asyncio.CancelledError()
    return task.exception() or RequiredLoopExitedError()


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
