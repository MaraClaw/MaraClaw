"""Dispatch helpers for trigger executions."""

from __future__ import annotations

import uuid
from datetime import datetime

from app.core.json_types import JsonObject, mapping_from_row
from app.core.logging import logger
from app.dao.trigger_dao import agent_trigger_dao
from app.db.session import connection_ctx
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime.executions import (
    build_execution_runtime_trigger,
    claim_pending_trigger_executions,
    fail_abandoned_trigger_executions,
)
from app.services.trigger_runtime.keys import POLL_OBSERVED_KEY, build_scheduled_execution_key, poll_sequence
from app.services.trigger_runtime.queue import enqueue_trigger_execution


def runtime_execution_payload(trigger: AgentTriggerRecord) -> JsonObject:
    """Capture ephemeral trigger evaluation context into an execution payload."""
    cfg = mapping_from_row(trigger.config)
    payload: JsonObject = {}
    for key in (
        "_matched_message",
        "_matched_message_id",
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


class PollBaselineLostError(Exception):
    """The conditional baseline update matched no row: another worker or a config edit advanced the sequence."""


async def _enqueue_poll_observation(trigger: AgentTriggerRecord, now: datetime) -> None:
    """Enqueue a poll change and advance its baseline and sequence in one transaction.

    The conditional update runs after `try_enqueue`, which must hold no other row locks. If it matches nothing the
    execution is rolled back, and this worker's observation is dropped instead of adopted. An enqueue failure
    leaves the baseline alone, so the next poll sees the same change again.
    """
    observed = trigger.config.get(POLL_OBSERVED_KEY)
    if not isinstance(observed, str):
        raise TypeError("Poll enqueue requires an observed value from evaluate_trigger")
    expected_seq = poll_sequence(trigger.config)
    try:
        async with connection_ctx() as db, db.raw.transaction():
            _ = await enqueue_trigger_execution(
                trigger=trigger,
                source=trigger.type,
                idempotency_key=build_scheduled_execution_key(trigger, now),
                payload_obj=runtime_execution_payload(trigger),
            )
            # No execution row (idempotency conflict) still advances: a key left over from a whole-config write
            # that reset the sequence would otherwise block every later change; a lost race fails the guard below.
            if not await agent_trigger_dao.advance_poll_baseline(trigger.id, expected_seq=expected_seq, value=observed):
                raise PollBaselineLostError
    except PollBaselineLostError:
        logger.info("Poll change for trigger {} dropped: baseline advanced concurrently", trigger.id)


async def enqueue_due_trigger(trigger: AgentTriggerRecord, now: datetime) -> None:
    if trigger.type == "poll":
        await _enqueue_poll_observation(trigger, now)
        return
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
