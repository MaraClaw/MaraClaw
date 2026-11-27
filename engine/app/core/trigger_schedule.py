"""When a time-based trigger can next be due: the one place ``agent_triggers.next_due_at`` is computed.

``next_due_at`` is the earliest moment ``evaluator.evaluate_trigger`` can say the trigger is due, so the daemon reads
only rows with ``next_due_at <= now`` instead of evaluating every enabled trigger. It mirrors that function's
conditions (schedule, cooldown, ``max_fires``, expiry); keep the two in step. ``None`` means the trigger can never be
due as stored: disabled, out of fires, malformed schedule, a spent ``once``, or a type that is not time-based.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from croniter import croniter

from app.core.json_types import JsonValue
from app.core.logging import logger
from app.records.trigger import AgentTriggerRecord

MIN_POLL_INTERVAL_MINUTES = 5

POLL_CHECKPOINT_KEY = "_poll_checked_at"
"""Persisted ISO time of the last claimed poll; written only by `claim_poll_checkpoint`, cleared on a source edit."""


def config_number(value: JsonValue | None) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def poll_checkpoint(cfg: Mapping[str, object]) -> str | None:
    """The stored checkpoint as the claim compares it: a string, or None when absent or not a string."""
    value = cfg.get(POLL_CHECKPOINT_KEY)
    return value if isinstance(value, str) else None


def configured_timezone(cfg: Mapping[str, object]) -> str | None:
    """The timezone a trigger's own config names, if any."""
    value = cfg.get("timezone")
    return value if isinstance(value, str) and value else None


def zone_info(name: str) -> ZoneInfo:
    """The named zone, UTC when it is unknown."""
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def poll_reference(trigger: AgentTriggerRecord, checkpoint: str | None) -> datetime | None:
    """The later of the last firing (or creation) and the last claimed poll: the interval counts from it."""
    reference = trigger.last_fired_at or trigger.created_at
    if checkpoint is None:
        return reference
    try:
        checked = datetime.fromisoformat(checkpoint)
    except ValueError:
        return reference
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return checked if reference is None else max(reference, checked)


def cron_next_run(expr: str, base: datetime, tz: ZoneInfo) -> datetime:
    """The first run of ``expr`` after ``base``, in ``tz``."""
    local_base = base.astimezone(tz) if base.tzinfo else base.replace(tzinfo=tz)
    next_run = cast("object", croniter(expr, local_base).get_next(datetime))
    if not isinstance(next_run, datetime):
        raise TypeError("croniter.get_next(datetime) must return a datetime")
    return next_run


def next_due_at(trigger: AgentTriggerRecord, agent_timezone: str) -> datetime | None:
    """The earliest instant the stored trigger can be due; ``agent_timezone`` is its agent's effective zone."""
    if not trigger.is_enabled:
        return None
    if trigger.max_fires is not None and trigger.fire_count >= trigger.max_fires:
        return None
    try:
        due = _scheduled_at(trigger, agent_timezone)
    except OverflowError, ValueError:
        return None
    if due is None:
        return None
    if trigger.last_fired_at:
        due = max(due, trigger.last_fired_at + timedelta(seconds=trigger.cooldown_seconds))
    if trigger.expires_at:
        due = min(due, trigger.expires_at)
    return due


def _scheduled_at(trigger: AgentTriggerRecord, agent_timezone: str) -> datetime | None:
    cfg = trigger.config
    base = trigger.last_fired_at or trigger.created_at
    match trigger.type:
        case "cron":
            expr = cfg.get("expr", "* * * * *")
            if not isinstance(expr, str) or base is None:
                return None
            zone = zone_info(configured_timezone(cfg) or agent_timezone)
            try:
                return cron_next_run(expr, base, zone).astimezone(UTC)
            except Exception as error:
                logger.warning(f"Invalid cron expr '{expr}' for trigger {trigger.name}: {error}")
                return None
        case "once":
            at = cfg.get("at")
            if not isinstance(at, str) or not at or trigger.fire_count != 0:
                return None
            moment = datetime.fromisoformat(at)
            return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        case "interval":
            minutes = config_number(cfg.get("minutes", 30))
            if minutes is None or base is None:
                return None
            return base + timedelta(minutes=minutes)
        case "poll":
            configured = config_number(cfg.get("interval_min", MIN_POLL_INTERVAL_MINUTES))
            reference = poll_reference(trigger, poll_checkpoint(cfg))
            if configured is None or reference is None:
                return None
            return reference + timedelta(minutes=max(configured, MIN_POLL_INTERVAL_MINUTES))
        case "on_message":
            # Matched against chat messages on every tick, so it is always due.
            return trigger.created_at
        case _:
            return None
