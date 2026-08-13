"""Execution claiming and completion helpers for distributed triggers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.dao.trigger_dao import trigger_execution_dao
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord, TriggerExecutionRecord

settings = get_settings()


async def mark_trigger_executions_completed(execution_ids: list[uuid.UUID]) -> None:
    if not execution_ids:
        return
    now = datetime.now(UTC)
    async with connection_ctx() as db:
        await db.execute(
            """
            UPDATE trigger_executions
            SET status = 'completed',
                finished_at = %(now)s,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_error = NULL
            WHERE id = ANY(%(ids)s)
            """,
            {"now": now, "ids": execution_ids},
        )


async def mark_trigger_executions_failed(execution_ids: list[uuid.UUID], error_text: str) -> None:
    if not execution_ids:
        return
    now = datetime.now(UTC)
    async with connection_ctx() as db:
        await db.execute(
            """
            UPDATE trigger_executions
            SET status = 'failed',
                finished_at = %(now)s,
                lease_owner = NULL,
                lease_expires_at = NULL,
                last_error = %(error_text)s
            WHERE id = ANY(%(ids)s)
            """,
            {"now": now, "ids": execution_ids, "error_text": error_text},
        )


async def claim_pending_trigger_executions(
    *,
    sources: list[str] | None = None,
    limit: int = 100,
) -> list[tuple[TriggerExecutionRecord, AgentTriggerRecord]]:
    now = datetime.now(UTC)
    lease_until = now + timedelta(minutes=5)
    sources = sources or ["webhook", "cron", "once", "interval", "poll", "on_message"]
    claimed_pairs: list[tuple[TriggerExecutionRecord, AgentTriggerRecord]] = []

    # Hold one transaction for SELECT ... FOR UPDATE + lease updates.
    async with connection_ctx() as db:
        rows = await trigger_execution_dao.claim_pending(sources=sources, now=now, limit=limit)
        if not rows:
            return []
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
                "owner": settings.INSTANCE_ID,
                "lease_until": lease_until,
                "ids": [execution.id for execution, _trigger in rows],
            },
        )
        for execution, trigger in rows:
            started_at = execution.started_at or now
            execution.status = "processing"
            execution.started_at = started_at
            execution.finished_at = None
            execution.lease_owner = settings.INSTANCE_ID
            execution.lease_expires_at = lease_until
            claimed_pairs.append((execution, trigger))
    return claimed_pairs


def build_execution_runtime_trigger(
    trigger: AgentTriggerRecord | Any,
    execution: TriggerExecutionRecord | Any,
) -> AgentTriggerRecord:
    runtime_cfg = {
        **(getattr(trigger, "config", None) or {}),
        "_execution_id": str(execution.id),
    }
    payload = getattr(execution, "payload", None)
    if payload:
        runtime_cfg.update(payload)
    payload_text = getattr(execution, "payload_text", None)
    if payload_text:
        runtime_cfg["_webhook_payload"] = payload_text
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


async def mark_base_triggers_fired(trigger_ids: list[uuid.UUID], now: datetime) -> None:
    if not trigger_ids:
        return
    async with connection_ctx() as db:
        await db.execute(
            """
            UPDATE agent_triggers
            SET last_fired_at = %(now)s,
                fire_count = fire_count + 1,
                is_enabled = CASE
                    WHEN type = 'once' THEN FALSE
                    WHEN max_fires IS NOT NULL AND fire_count + 1 >= max_fires THEN FALSE
                    ELSE is_enabled
                END
            WHERE id = ANY(%(ids)s)
            """,
            {"now": now, "ids": trigger_ids},
        )
