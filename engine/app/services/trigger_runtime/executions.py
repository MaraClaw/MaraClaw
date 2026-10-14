"""Execution claiming and completion helpers for distributed triggers."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.core.json_types import json_as_str, json_object_from
from app.core.logging import get_trace_id, logger, set_trace_id
from app.dao.trigger_dao import agent_trigger_dao, trigger_execution_dao
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord

settings = get_settings()
LEASE_DURATION = timedelta(minutes=5)
LEASE_RENEW_INTERVAL = timedelta(seconds=60)


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
        if execution_id and token:
            grouped.setdefault(token, []).append(uuid.UUID(execution_id))
    return [ExecutionLease(token, tuple(ids)) for token, ids in grouped.items()]


async def renew_trigger_execution_lease(lease: ExecutionLease) -> list[uuid.UUID]:
    async with connection_ctx():
        renewed = await trigger_execution_dao.renew_lease(
            lease.execution_ids, lease_token=lease.token, lease_until=datetime.now(UTC) + LEASE_DURATION
        )
    lost = set(lease.execution_ids) - set(renewed)
    if lost:
        logger.warning(f"Execution lease lost; renewal skipped for {list(lost)}")
    return renewed


async def mark_trigger_executions_completed(leases: Sequence[ExecutionLease]) -> list[uuid.UUID]:
    now = datetime.now(UTC)
    completed: list[uuid.UUID] = []
    for lease in leases:
        changed = await trigger_execution_dao.finish(
            lease.execution_ids, lease_token=lease.token, status="completed", error_text=None, now=now
        )
        lost = set(lease.execution_ids) - set(changed)
        if lost:
            logger.warning(f"Execution lease lost; completion skipped for {list(lost)}")
        completed.extend(changed)
    return completed


async def mark_trigger_executions_failed(leases: Sequence[ExecutionLease], error_text: str) -> list[uuid.UUID]:
    now = datetime.now(UTC)
    failed: list[uuid.UUID] = []
    for lease in leases:
        changed = await trigger_execution_dao.finish(
            lease.execution_ids, lease_token=lease.token, status="failed", error_text=error_text, now=now
        )
        lost = set(lease.execution_ids) - set(changed)
        if lost:
            logger.warning(f"Execution lease lost; failure skipped for {list(lost)}")
        failed.extend(changed)
    return failed


async def _renew_until_lost(leases: list[ExecutionLease], trace_id: str | None) -> None:
    if trace_id:
        set_trace_id(trace_id)
    held = leases
    while held:
        await asyncio.sleep(LEASE_RENEW_INTERVAL.total_seconds())
        remaining: list[ExecutionLease] = []
        for lease in held:
            try:
                renewed = await renew_trigger_execution_lease(lease)
            except Exception as exc:
                logger.warning(f"Execution lease renewal failed for {lease.execution_ids}: {exc}")
                remaining.append(lease)
                continue
            lost = set(lease.execution_ids) - set(renewed)
            if lost:
                logger.warning(f"Execution lease lost; stop renewing {list(lost)}")
            if renewed:
                remaining.append(ExecutionLease(lease.token, tuple(renewed)))
        held = remaining


@asynccontextmanager
async def hold_trigger_execution_leases(leases: Sequence[ExecutionLease]) -> AsyncIterator[None]:
    if not leases:
        yield
        return
    task = asyncio.create_task(_renew_until_lost(list(leases), get_trace_id()), context=contextvars.Context())
    try:
        yield
    finally:
        task.cancel()
        joined = asyncio.gather(task, return_exceptions=True)
        interrupted: asyncio.CancelledError | None = None
        while True:
            try:
                _ = await asyncio.shield(joined)
                break
            except asyncio.CancelledError as exc:
                interrupted = exc
        if interrupted is not None:
            raise interrupted


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
            execution.status = "processing"
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
    if execution.lease_owner:
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
        last_fired_at=getattr(trigger, "last_fired_at", None),
        fire_count=int(getattr(trigger, "fire_count", 0) or 0),
        max_fires=getattr(trigger, "max_fires", None),
        cooldown_seconds=int(getattr(trigger, "cooldown_seconds", 60) or 60),
        is_system=bool(getattr(trigger, "is_system", False)),
        created_at=getattr(trigger, "created_at", None),
        expires_at=getattr(trigger, "expires_at", None),
    )
