"""Dispatch helpers for trigger executions."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.core.json_types import JsonObject, mapping_from_row
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime.executions import (
    build_execution_runtime_trigger,
    claim_pending_trigger_executions,
    fail_abandoned_trigger_executions,
)
from app.services.trigger_runtime.keys import build_scheduled_execution_key
from app.services.trigger_runtime.queue import enqueue_trigger_execution


def runtime_execution_payload(trigger: AgentTriggerRecord) -> JsonObject:
    """Capture ephemeral trigger evaluation context into an execution payload."""
    cfg = mapping_from_row(trigger.config)
    payload: JsonObject = {}
    for key in (
        "_matched_message",
        "_matched_from",
        "okr_member_id",
        "okr_member_type",
        "okr_report_date",
        "_notification_summary",
        "_origin_session_id",
        "_origin_user_id",
        "_origin_source_channel",
        "_a2a_session_id",
    ):
        if key in cfg and cfg.get(key) is not None:
            payload[key] = cfg.get(key)
    return payload


async def enqueue_due_trigger(trigger: AgentTriggerRecord, now: datetime) -> None:
    _ = await enqueue_trigger_execution(
        trigger=trigger,
        source=trigger.type,
        idempotency_key=build_scheduled_execution_key(trigger, now),
        payload_obj=runtime_execution_payload(trigger),
    )


async def claim_ready_trigger_invocations(
    now: datetime,
) -> tuple[dict[uuid.UUID, list[AgentTriggerRecord]], set[uuid.UUID]]:
    del now
    fired_by_agent: dict[uuid.UUID, list[AgentTriggerRecord]] = {}
    force_invoke_agents: set[uuid.UUID] = set()

    _ = await fail_abandoned_trigger_executions()
    claimed_executions = await claim_pending_trigger_executions()

    for execution, trigger in claimed_executions:
        runtime_trigger = build_execution_runtime_trigger(trigger, execution)
        fired_by_agent.setdefault(trigger.agent_id, []).append(runtime_trigger)
        force_invoke_agents.add(trigger.agent_id)

    return fired_by_agent, force_invoke_agents
