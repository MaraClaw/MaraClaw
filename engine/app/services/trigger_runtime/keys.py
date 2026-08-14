"""Deterministic idempotency keys for trigger executions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from croniter import croniter

from app.core.json_types import json_object_from
from app.records.trigger import AgentTriggerRecord


def build_scheduled_execution_key(trigger: AgentTriggerRecord, now: datetime) -> str:
    """Build a deterministic idempotency key for non-webhook trigger runs."""
    cfg = json_object_from(trigger.config)
    trigger_type = trigger.type

    if trigger_type == "once":
        return f"once:{trigger.id}:{cfg.get('at', '')}"

    if trigger_type == "interval":
        configured_minutes = cfg.get("minutes", 30)
        if isinstance(configured_minutes, bool) or not isinstance(configured_minutes, int | float):
            raise ValueError("Trigger interval minutes must be numeric")
        minutes = int(configured_minutes)
        base = trigger.last_fired_at or trigger.created_at
        if base is None:
            raise ValueError("Trigger interval requires a created_at timestamp")
        due_at = base + timedelta(minutes=minutes)
        return f"interval:{trigger.id}:{due_at.astimezone(UTC).isoformat()}"

    if trigger_type == "cron":
        expr = cfg.get("expr", "* * * * *")
        if not isinstance(expr, str):
            raise ValueError("Trigger cron expression must be a string")
        base = trigger.last_fired_at or trigger.created_at
        cron = croniter(expr, base)
        due_at = cron.get_next(datetime)
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        return f"cron:{trigger.id}:{due_at.astimezone(UTC).isoformat()}"

    if trigger_type == "on_message":
        matched_from = str(cfg.get("_matched_from") or "")
        matched_message = str(cfg.get("_matched_message") or "")
        digest = hashlib.sha256(f"{matched_from}\n{matched_message}".encode()).hexdigest()
        return f"on_message:{trigger.id}:{digest}"

    if trigger_type == "poll":
        current_value = str(cfg.get("_last_value") or "")
        digest = hashlib.sha256(current_value.encode("utf-8")).hexdigest()
        return f"poll:{trigger.id}:{digest}"

    return f"{trigger_type}:{trigger.id}:{now.replace(microsecond=0).isoformat()}"
