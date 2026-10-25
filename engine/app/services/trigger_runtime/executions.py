"""Execution claiming and completion helpers for distributed triggers."""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

from app.config import get_settings
from app.core.domain.triggers import TriggerExecutionStatus
from app.core.json_types import json_as_str, json_object_from
from app.core.logging import get_trace_id, logger, set_trace_id
from app.dao.trigger_dao import agent_trigger_dao, trigger_execution_dao
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord

settings = get_settings()
LEASE_DURATION = timedelta(minutes=5)
LEASE_RENEW_INTERVAL = timedelta(seconds=60)
LEASE_STOP_MARGIN = timedelta(seconds=60)
LEASE_EXPIRED_ERROR = "Execution lease expired after its trigger was disabled"
LEASE_LOST_ERROR = "Execution lease lost before the invocation finished"


class ExecutionLeaseLostError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason: str = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    token: str
    execution_ids: tuple[uuid.UUID, ...]


def new_lease_token() -> str:
    return f"{settings.INSTANCE_ID[:95]}:{uuid.uuid4().hex}"


def trigger_execution_leases(triggers: Sequence[AgentTriggerRecord]) -> list[ExecutionLease]:
    grouped: dict[str, list[uuid.UUID]] = {}
    for trigger in triggers:
        cfg = json_object_from(trigger.config)
        execution_id = json_as_str(cfg.get("_execution_id"))
        token = json_as_str(cfg.get("_lease_token"))
        if execution_id not in (None, "") and token not in (None, ""):
            grouped.setdefault(token, []).append(uuid.UUID(execution_id))
    return [ExecutionLease(token, tuple(ids)) for token, ids in grouped.items()]


async def _await_in_fresh_context[T](coroutine: Coroutine[None, None, T]) -> T:
    """Keep lease writes outside ambient transactions while preserving the trace id."""
    context = contextvars.Context()
    trace_id = get_trace_id()
    if trace_id not in (None, ""):
        context.run(set_trace_id, trace_id)
    task = asyncio.create_task(coroutine, context=context)
    return await task


async def renew_trigger_execution_lease(lease: ExecutionLease) -> list[uuid.UUID]:
    async def renew() -> list[uuid.UUID]:
        lease_until = datetime.now(UTC) + LEASE_DURATION
        return [
            execution_id
            for execution_id in lease.execution_ids
            if await trigger_execution_dao.renew_lease(execution_id, lease_token=lease.token, lease_until=lease_until)
        ]

    renewed = await _await_in_fresh_context(renew())
    lost = set(lease.execution_ids) - set(renewed)
    if lost:
        logger.warning(f"Execution lease lost; renewal skipped for {list(lost)}")
    return renewed


async def mark_trigger_executions_completed(leases: Sequence[ExecutionLease]) -> list[uuid.UUID]:
    async def complete() -> list[uuid.UUID]:
        now = datetime.now(UTC)
        completed: list[uuid.UUID] = []
        for lease in leases:
            changed = [
                execution_id
                for execution_id in lease.execution_ids
                if await trigger_execution_dao.finish(
                    execution_id,
                    lease_token=lease.token,
                    status=TriggerExecutionStatus.COMPLETED,
                    error_text=None,
                    now=now,
                )
            ]
            lost = set(lease.execution_ids) - set(changed)
            if lost:
                logger.warning(f"Execution lease lost; completion skipped for {list(lost)}")
            completed.extend(changed)
        return completed

    return await _await_in_fresh_context(complete())


async def mark_trigger_executions_failed(leases: Sequence[ExecutionLease], error_text: str) -> list[uuid.UUID]:
    async def fail() -> list[uuid.UUID]:
        now = datetime.now(UTC)
        failed: list[uuid.UUID] = []
        for lease in leases:
            changed = [
                execution_id
                for execution_id in lease.execution_ids
                if await trigger_execution_dao.finish(
                    execution_id,
                    lease_token=lease.token,
                    status=TriggerExecutionStatus.FAILED,
                    error_text=error_text,
                    now=now,
                )
            ]
            lost = set(lease.execution_ids) - set(changed)
            if lost:
                logger.warning(f"Execution lease lost; failure skipped for {list(lost)}")
            failed.extend(changed)
        return failed

    return await _await_in_fresh_context(fail())


async def fail_abandoned_trigger_executions() -> list[uuid.UUID]:
    failed = await trigger_execution_dao.fail_expired_for_disabled_triggers(
        now=datetime.now(UTC), error_text=LEASE_EXPIRED_ERROR
    )
    if failed:
        logger.warning(f"Failed expired executions for disabled triggers: {failed}")
    return failed


class ExecutionLeaseHold:
    def __init__(self, leases: Sequence[ExecutionLease]) -> None:
        self.leases: tuple[ExecutionLease, ...] = tuple(leases)
        self.loop: asyncio.AbstractEventLoop
        self.owner_task: asyncio.Task[object]
        self.cancelling_baseline: int = 0
        self.lock: asyncio.Lock = asyncio.Lock()
        self.deadline: float = 0.0
        self.wall_deadline: float = 0.0
        self.closed: bool = False
        self.fired_reason: str | None = None
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        if not self.leases:
            return self
        self.loop = asyncio.get_running_loop()
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Execution lease hold requires an asyncio task")
        self.owner_task = owner
        self.cancelling_baseline = owner.cancelling()
        window = (LEASE_DURATION - LEASE_STOP_MARGIN).total_seconds()
        bound = self.loop.time() + window
        wall_bound = time.time() + window
        await self._renew_batch(bound, wall_bound)
        context = contextvars.Context()
        trace_id = get_trace_id()
        if trace_id not in (None, ""):
            context.run(set_trace_id, trace_id)
        self.heartbeat_task = asyncio.create_task(self._heartbeat(), context=context)
        return self

    def ensure_held(self) -> None:
        if not self.leases:
            return
        if self.fired_reason is not None:
            raise ExecutionLeaseLostError(self.fired_reason)
        if self.loop.time() >= self.deadline or time.time() >= self.wall_deadline:
            raise ExecutionLeaseLostError("lease not renewed before it could expire")

    async def _renew_batch(self, bound: float, wall_bound: float) -> None:
        try:
            async with asyncio.timeout_at(bound):
                async with self.lock:
                    start = self.loop.time()
                    start_wall = time.time()
                    if start >= bound or start_wall >= wall_bound:
                        raise ExecutionLeaseLostError("lease not renewed before it could expire")
                    window = (LEASE_DURATION - LEASE_STOP_MARGIN).total_seconds()
                    deadline = start + window
                    wall_deadline = start_wall + window
                    lost: list[uuid.UUID] = []
                    for lease in self.leases:
                        renewed = await renew_trigger_execution_lease(lease)
                        lost.extend(execution_id for execution_id in lease.execution_ids if execution_id not in renewed)
                    if self.loop.time() >= bound or time.time() >= wall_bound:
                        raise ExecutionLeaseLostError("lease not renewed before it could expire")
                    if lost:
                        raise ExecutionLeaseLostError(f"lease lost for {lost}")
                    self.deadline = deadline
                    self.wall_deadline = wall_deadline
        except TimeoutError as exc:
            if self.loop.time() < bound:
                raise
            raise ExecutionLeaseLostError("lease not renewed before it could expire") from exc

    def _fire(self, reason: str) -> None:
        if not self.closed and self.fired_reason is None:
            self.fired_reason = reason
            logger.warning(f"Stopping trigger invocation: {reason}")
            _ = self.owner_task.cancel()

    async def _heartbeat(self) -> None:
        while not self.closed:
            delay = min(LEASE_RENEW_INTERVAL.total_seconds(), self.deadline - self.loop.time())
            await asyncio.sleep(max(0, delay))
            if self.loop.time() >= self.deadline or time.time() >= self.wall_deadline:
                self._fire("lease not renewed before it could expire")
                return
            try:
                await self._renew_batch(self.deadline, self.wall_deadline)
            except ExecutionLeaseLostError as exc:
                self._fire(exc.reason)
                return
            except TimeoutError:
                self._fire("lease not renewed before it could expire")
                return
            except Exception as exc:
                logger.warning(f"Execution lease renewal failed: {exc}")

    async def confirm(self) -> None:
        if self.leases:
            await self._renew_batch(self.deadline, self.wall_deadline)

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
            raise ExecutionLeaseLostError(self.fired_reason) from exc
        if interrupted is not None:
            raise interrupted


def hold_trigger_execution_leases(leases: Sequence[ExecutionLease]) -> ExecutionLeaseHold:
    return ExecutionLeaseHold(leases)


async def claim_pending_trigger_executions(
    *,
    sources: list[str] | None = None,
    limit: int = 100,
) -> list[tuple[TriggerExecutionRecord, AgentTriggerRecord]]:
    now = datetime.now(UTC)
    lease_until = now + LEASE_DURATION
    lease_token = new_lease_token()
    sources = sources or ["webhook", "cron", "once", "interval", "poll", "on_message"]
    claimed_pairs: list[tuple[TriggerExecutionRecord, AgentTriggerRecord]] = []

    # Hold one transaction for SELECT ... FOR UPDATE + lease updates.
    async with connection_ctx() as db:
        rows = await trigger_execution_dao.claim_pending(sources=sources, now=now, limit=limit)
        if not rows:
            return []
        first_start_ids = [execution.id for execution, _trigger in rows if execution.started_at is None]
        await db.execute(
            """
            UPDATE trigger_executions
            SET status = 'processing',
                started_at = COALESCE(started_at, %(now)s),
                finished_at = NULL,
                lease_owner = %(owner)s,
                lease_expires_at = %(lease_until)s,
                last_error = NULL
            WHERE id = ANY(%(ids)s)
            """,
            {
                "now": now,
                "owner": lease_token,
                "lease_until": lease_until,
                "ids": [execution.id for execution, _trigger in rows],
            },
        )
        await agent_trigger_dao.record_first_starts(first_start_ids, now=now)
        for execution, trigger in rows:
            started_at = execution.started_at or now
            execution.status = TriggerExecutionStatus.PROCESSING
            execution.started_at = started_at
            execution.finished_at = None
            execution.lease_owner = lease_token
            execution.lease_expires_at = lease_until
            claimed_pairs.append((execution, trigger))
    return claimed_pairs


def build_execution_runtime_trigger(
    trigger: AgentTriggerRecord,
    execution: TriggerExecutionRecord,
) -> AgentTriggerRecord:
    runtime_cfg = dict(json_object_from(trigger.config))
    payload = json_object_from(execution.payload)
    if payload:
        runtime_cfg.update(payload)
    payload_text = execution.payload_text
    if payload_text:
        runtime_cfg["_webhook_payload"] = payload_text
    runtime_cfg["_execution_id"] = str(execution.id)
    if execution.lease_owner not in (None, ""):
        runtime_cfg["_lease_token"] = execution.lease_owner
    return AgentTriggerRecord(
        id=trigger.id,
        agent_id=trigger.agent_id,
        name=trigger.name,
        type=trigger.type,
        config=runtime_cfg,
        reason=getattr(trigger, "reason", "") or "",
        focus_ref=getattr(trigger, "focus_ref", None),
        is_enabled=bool(getattr(trigger, "is_enabled", True)),
        self_disabled=bool(getattr(trigger, "self_disabled", False)),
        last_fired_at=getattr(trigger, "last_fired_at", None),
        fire_count=int(getattr(trigger, "fire_count", 0) or 0),
        max_fires=getattr(trigger, "max_fires", None),
        cooldown_seconds=int(getattr(trigger, "cooldown_seconds", 60) or 60),
        is_system=bool(getattr(trigger, "is_system", False)),
        created_at=getattr(trigger, "created_at", None),
        expires_at=getattr(trigger, "expires_at", None),
    )
