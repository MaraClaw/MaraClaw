"""Owned, transaction-isolated lease renewal for periodic schedule occurrences."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from contextvars import Context
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID

from app.core.logging import get_trace_id, logger, set_trace_id
from app.dao.schedule_dao import agent_schedule_dao
from app.services.llm.background_guard import ScheduleLeaseLostError

LEASE_DURATION = timedelta(minutes=5)
LEASE_RENEW_INTERVAL = timedelta(seconds=60)
LEASE_STOP_MARGIN = timedelta(seconds=60)
_EXPIRED = "Schedule occurrence lease not renewed before it could expire"


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
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> ScheduleLeaseHold:
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

    def ensure_held(self) -> None:
        if self.fired_reason is not None:
            raise ScheduleLeaseLostError(self.fired_reason)
        if self.loop.time() >= self.deadline or time.time() >= self.wall_deadline:
            raise ScheduleLeaseLostError(_EXPIRED)

    async def _renew(self, bound: float, wall_bound: float) -> None:
        try:
            async with asyncio.timeout_at(bound):
                async with self.lock:
                    start, start_wall = self.loop.time(), time.time()
                    if start >= bound or start_wall >= wall_bound:
                        raise ScheduleLeaseLostError(_EXPIRED)
                    renewed = await in_fresh_schedule_context(
                        agent_schedule_dao.renew_occurrence(self.schedule_id, self.token, datetime.now(UTC))
                    )
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

    async def confirm(self) -> None:
        self.ensure_held()
        await self._renew(self.deadline, self.wall_deadline)
        self.ensure_held()

    def _fire(self, reason: str) -> None:
        if not self.closed and self.fired_reason is None:
            self.fired_reason = reason
            logger.bind(schedule_id=str(self.schedule_id)).warning("Stopping periodic schedule: {}", reason)
            _ = self.owner_task.cancel()

    async def _heartbeat(self) -> None:
        while not self.closed:
            delay = min(LEASE_RENEW_INTERVAL.total_seconds(), self.deadline - self.loop.time())
            await asyncio.sleep(max(0, delay))
            try:
                self.ensure_held()
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
