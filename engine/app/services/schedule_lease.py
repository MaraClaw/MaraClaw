"""Owned, transaction-isolated lease renewal for periodic schedule occurrences."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import Context
from datetime import UTC, datetime, timedelta
from functools import partial
from types import TracebackType
from typing import Self
from uuid import UUID

from app.core.logging import get_trace_id, logger, set_trace_id
from app.dao.schedule_dao import agent_schedule_dao
from app.services.llm.background_guard import ScheduleLeaseLostError

LEASE_DURATION = timedelta(minutes=5)
LEASE_RENEW_INTERVAL = timedelta(seconds=60)
LEASE_STOP_MARGIN = timedelta(seconds=60)
_EXPIRED = "Schedule occurrence lease not renewed before it could expire"
SCHEDULE_RUN_TIMEOUT_ERROR = "Scheduled run ran out of its time budget"


class ScheduleRunTimeoutError(ScheduleLeaseLostError):
    """The run budget ran out while the lease was still owned.

    It subclasses the lease error so every guarded checkpoint stops the run; only the hold owner tells them apart.
    """


def _trace_context() -> Context:
    context = Context()
    trace_id = get_trace_id()
    if trace_id not in (None, ""):
        context.run(set_trace_id, trace_id)
    return context


async def in_fresh_schedule_context[T](coroutine: Coroutine[None, None, T]) -> T:
    return await asyncio.create_task(coroutine, context=_trace_context())


class ScheduleLeaseHold:
    def __init__(self, schedule_id: UUID, token: UUID) -> None:
        self.schedule_id: UUID = schedule_id
        self.token: UUID = token
        self.loop: asyncio.AbstractEventLoop
        self.owner_task: asyncio.Task[object]
        self.cancelling_baseline: int = 0
        self.lock: asyncio.Lock = asyncio.Lock()
        self.deadline: float = 0.0
        self.wall_deadline: float = 0.0
        self.closed: bool = False
        self.fired_reason: str | None = None
        self.timeout_reason: str | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.loop = asyncio.get_running_loop()
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Schedule lease hold requires an asyncio task")
        self.owner_task = owner
        self.cancelling_baseline = owner.cancelling()
        window = (LEASE_DURATION - LEASE_STOP_MARGIN).total_seconds()
        await self._renew(self.loop.time() + window, time.time() + window)
        self.heartbeat_task = asyncio.create_task(self._heartbeat(), context=_trace_context())
        return self

    def _lapsed(self) -> bool:
        return self.loop.time() >= self.deadline or time.time() >= self.wall_deadline

    def ensure_owned(self) -> None:
        """Raise ``ScheduleLeaseLostError`` once ownership is lost; a spent budget still owns the lease."""
        if self.fired_reason is not None:
            raise ScheduleLeaseLostError(self.fired_reason)
        if self._lapsed():
            raise ScheduleLeaseLostError(_EXPIRED)

    def ensure_held(self) -> None:
        self.ensure_owned()
        if self.timeout_reason is not None:
            raise ScheduleRunTimeoutError(self.timeout_reason)

    async def _renew(self, bound: float, wall_bound: float) -> None:
        try:
            async with asyncio.timeout_at(bound):
                async with self.lock:
                    start, start_wall = self.loop.time(), time.time()
                    if start >= bound or start_wall >= wall_bound:
                        raise ScheduleLeaseLostError(_EXPIRED)
                    renewed = await in_fresh_schedule_context(self.renew_ownership(datetime.now(UTC)))
                    if self.loop.time() >= bound or time.time() >= wall_bound:
                        raise ScheduleLeaseLostError(_EXPIRED)
                    if not renewed:
                        raise ScheduleLeaseLostError("Schedule occurrence ownership was revoked")
                    window = (LEASE_DURATION - LEASE_STOP_MARGIN).total_seconds()
                    self.deadline = start + window
                    self.wall_deadline = start_wall + window
        except TimeoutError as exc:
            if self.loop.time() < bound:
                raise
            raise ScheduleLeaseLostError(_EXPIRED) from exc

    async def renew_ownership(self, now: datetime) -> bool:
        return await agent_schedule_dao.renew_occurrence(self.schedule_id, self.token, now)

    async def confirm(self) -> None:
        self.ensure_held()
        await self._renew(self.deadline, self.wall_deadline)
        self.ensure_held()

    def _fire(self, reason: str, *, timed_out: bool = False) -> None:
        """Cancel the owner once per cause: lost ownership, or a budget spent while the lease is still valid."""
        if self.closed or self.fired_reason is not None:
            return
        if timed_out and not self._lapsed():
            if self.timeout_reason is not None:
                return
            self.timeout_reason = reason
        else:
            reason = _EXPIRED if timed_out else reason
            self.fired_reason = reason
        logger.bind(schedule_id=str(self.schedule_id)).warning("Stopping schedule execution: {}", reason)
        _ = self.owner_task.cancel()

    @asynccontextmanager
    async def within_budget(self, seconds: float) -> AsyncGenerator[None]:
        """Stop the body through ``_fire`` after ``seconds`` and leave it with ``ScheduleRunTimeoutError``.

        The heartbeat keeps renewing, so the owner can still ``settle`` the timeout. Lost ownership and external
        cancellation win over a spent budget.
        """
        timer = self.loop.call_later(seconds, partial(self._fire, SCHEDULE_RUN_TIMEOUT_ERROR, timed_out=True))
        try:
            yield
        except BaseException as exc:
            outcome = self._budget_exit(exc)
            if outcome is None or outcome is exc:
                raise
            raise outcome from exc
        else:
            outcome = self._budget_exit(None)
            if outcome is not None:
                raise outcome
        finally:
            _ = timer.cancel()

    def _budget_exit(self, exc: BaseException | None) -> BaseException | None:
        """Take back the budget's own cancellation and choose what leaves the budget."""
        if self.timeout_reason is None:
            return exc
        hold_cancels = self.cancelling_baseline + (0 if self.fired_reason is None else 1)
        if self.owner_task.uncancel() > hold_cancels:
            return exc if isinstance(exc, asyncio.CancelledError) else asyncio.CancelledError()
        if self.fired_reason is not None:
            return exc if exc is not None else ScheduleLeaseLostError(self.fired_reason)
        return exc if isinstance(exc, ScheduleRunTimeoutError) else ScheduleRunTimeoutError(self.timeout_reason)

    async def settle[T](self, write: Callable[[], Coroutine[None, None, T]]) -> T:
        """Write the terminal outcome while the lease is owned and still renewing, then stop renewing.

        The renewal lock keeps the heartbeat from renewing concurrently, so it cannot see the write as revocation.
        """
        async with self.lock:
            self.ensure_owned()
            result = await in_fresh_schedule_context(write())
            self.closed = True
            if self.heartbeat_task is not None:
                _ = self.heartbeat_task.cancel()
        return result

    async def _heartbeat(self) -> None:
        while not self.closed:
            delay = min(LEASE_RENEW_INTERVAL.total_seconds(), self.deadline - self.loop.time())
            await asyncio.sleep(max(0, delay))
            try:
                self.ensure_owned()
                await self._renew(self.deadline, self.wall_deadline)
            except ScheduleLeaseLostError as exc:
                self._fire(str(exc))
                return
            except TimeoutError:
                self._fire(_EXPIRED)
                return
            except Exception as exc:
                logger.bind(schedule_id=str(self.schedule_id)).warning("Schedule lease renewal failed: {}", exc)

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.closed = True
        interrupted: asyncio.CancelledError | None = None
        if self.heartbeat_task is not None:
            _ = self.heartbeat_task.cancel()
            joined = asyncio.gather(self.heartbeat_task, return_exceptions=True)
            while True:
                try:
                    _ = await asyncio.shield(joined)
                    break
                except asyncio.CancelledError as cancelled:
                    interrupted = cancelled
        if self.fired_reason is not None:
            if self.owner_task.uncancel() > self.cancelling_baseline:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise interrupted if interrupted is not None else asyncio.CancelledError()
            raise ScheduleLeaseLostError(self.fired_reason) from exc
        if interrupted is not None:
            raise interrupted
