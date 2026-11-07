"""Deterministic idempotency keys for trigger executions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from croniter import croniter

from app.core.json_types import json_object_from
from app.records.trigger import AgentTriggerRecord

POLL_OBSERVED_KEY = "_poll_observed"
"""In-memory only: the observed value `evaluate_trigger` hands to `enqueue_due_trigger`; never persisted."""


POLL_CHECKPOINT_KEY = "_poll_checked_at"
"""Persisted ISO time of the last claimed poll; written only by `claim_poll_checkpoint`, cleared on a source edit."""


def poll_checkpoint(cfg: Mapping[str, object]) -> str | None:
    """The stored checkpoint as the claim compares it: a string, or None when absent or not a string."""
    value = cfg.get(POLL_CHECKPOINT_KEY)
    return value if isinstance(value, str) else None


def poll_sequence(cfg: Mapping[str, object]) -> int:
    """Stored per-trigger poll sequence; an absent or malformed value counts as 0 (legacy rows)."""
    value = cfg.get("_poll_seq")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


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
        message_id = cfg.get("_matched_message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Trigger on_message requires the matched chat message id")
        return f"on_message:{trigger.id}:{message_id}"

    if trigger_type == "poll":
        return f"poll:{trigger.id}:{poll_sequence(cfg) + 1}"

    return f"{trigger_type}:{trigger.id}:{now.replace(microsecond=0).isoformat()}"
