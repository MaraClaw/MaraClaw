"""Trigger daemon orchestrator.

Trigger-specific evaluation and invocation behavior now lives under
`app.services.trigger_runtime`. This module owns the main loop, dedup window,
and distributed claim/invoke flow.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.json_types import json_loads_value, mapping_from_row
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.records.trigger import AgentTriggerRecord
from app.services.trigger_runtime import (
    claim_ready_trigger_invocations,
    enqueue_due_trigger,
)
from app.services.trigger_runtime.evaluator import (
    evaluate_trigger as evaluate_trigger_runtime,
    handle_okr_collection_trigger as handle_okr_collection_trigger_runtime,
    handle_okr_report_trigger as handle_okr_report_trigger_runtime,
    mark_trigger_fired as mark_trigger_fired_runtime,
    mark_trigger_skipped as mark_trigger_skipped_runtime,
    should_skip_non_workday as should_skip_non_workday_runtime,
)
from app.services.trigger_runtime.invoker import invoke_agent_for_triggers as invoke_agent_for_triggers_runtime

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 30  # seconds - same agent won't be invoked twice within this window
MIN_POLL_INTERVAL_MINUTES = 5  # minimum poll interval to prevent abuse

# Safety: per-agent on_message fire rate limiter
_ON_MSG_RATE_WINDOW = 3600  # 1 hour window
_ON_MSG_RATE_LIMIT = 30  # max on_message fires per agent per hour
_on_msg_fire_log: dict[uuid.UUID, list[datetime]] = {}  # agent_id -> list of fire timestamps

_last_invoke: dict[uuid.UUID, datetime] = {}

_A2A_WAKE_CHAIN: dict[str, int] = {}
_A2A_WAKE_CHAIN_TTL = 300
_A2A_MAX_WAKE_DEPTH = 3
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _observe_background_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Trigger invocation failed")


def _cleanup_stale_invoke_cache():
    now = datetime.now(UTC)
    stale = [k for k, v in _last_invoke.items() if (now - v).total_seconds() > DEDUP_WINDOW * 2]
    for k in stale:
        del _last_invoke[k]
    # Clean up old on_message rate limiter entries
    cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
    stale_agents: list[uuid.UUID] = []
    for aid, timestamps in _on_msg_fire_log.items():
        _on_msg_fire_log[aid] = [t for t in timestamps if t > cutoff]
        if not _on_msg_fire_log[aid]:
            stale_agents.append(aid)
    for aid in stale_agents:
        del _on_msg_fire_log[aid]


async def _should_skip_non_workday(trigger: AgentTriggerRecord, local_now: datetime) -> bool:
    return await should_skip_non_workday_runtime(trigger, local_now)


async def _mark_trigger_skipped(trigger_id: uuid.UUID, now: datetime) -> None:
    await mark_trigger_skipped_runtime(trigger_id, now)


async def _mark_trigger_fired(trigger_id: uuid.UUID, now: datetime) -> None:
    await mark_trigger_fired_runtime(trigger_id, now)


async def _handle_okr_report_trigger(trigger: AgentTriggerRecord, now: datetime) -> bool:
    return await handle_okr_report_trigger_runtime(trigger, now)


async def _handle_okr_collection_trigger(trigger: AgentTriggerRecord, now: datetime) -> bool:
    return await handle_okr_collection_trigger_runtime(trigger, now)


async def _evaluate_trigger(trigger: AgentTriggerRecord, now: datetime) -> bool:
    return await evaluate_trigger_runtime(trigger, now)


async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTriggerRecord]):
    _ = new_trace_id()
    await invoke_agent_for_triggers_runtime(agent_id, triggers)


# ── Main Tick Loop ──────────────────────────────────────────────────


async def _tick():
    """One daemon tick: evaluate all triggers, group by agent, invoke."""
    _ = new_trace_id()
    now = datetime.now(UTC)

    all_triggers = await agent_trigger_dao.list_enabled()
    if not all_triggers:
        return

    # Evaluate and enqueue due triggers. Agent invocation happens only after
    # executions are claimed through the distributed execution queue.
    for trigger in all_triggers:
        # Auto-disable expired triggers
        if trigger.expires_at and now >= trigger.expires_at:
            t = await agent_trigger_dao.get(trigger.id)
            if t:
                _ = await agent_trigger_dao.update(db_obj=t, obj_in={"is_enabled": False})
            continue

        try:
            if await _evaluate_trigger(trigger, now):
                handled = await _handle_okr_report_trigger(trigger, now)
                if not handled:
                    handled = await _handle_okr_collection_trigger(trigger, now)
                if not handled:
                    # Fix 3: Rate limit on_message triggers per agent
                    if trigger.type == "on_message":
                        agent_fires = _on_msg_fire_log.get(trigger.agent_id, [])
                        cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
                        recent = [t for t in agent_fires if t > cutoff]
                        if len(recent) >= _ON_MSG_RATE_LIMIT:
                            logger.warning(
                                f"[A2A Safety] Agent {trigger.agent_id} hit "
                                + f"on_message rate limit ({_ON_MSG_RATE_LIMIT}/hr). "
                                + f"Auto-disabling trigger '{trigger.name}'."
                            )
                            t_obj = await agent_trigger_dao.get(trigger.id)
                            if t_obj:
                                _ = await agent_trigger_dao.update(db_obj=t_obj, obj_in={"is_enabled": False})
                            continue
                        recent.append(now)
                        _on_msg_fire_log[trigger.agent_id] = recent
                    await enqueue_due_trigger(trigger, now)
        except Exception as e:
            logger.warning(f"Error evaluating trigger {trigger.name}: {e}")

    # Claim queued executions with a DB lease so only one worker handles each event.
    try:
        fired_by_agent, force_invoke_agents = await claim_ready_trigger_invocations(now)
    except Exception as e:
        logger.warning(f"Failed to claim trigger executions: {e}")
        fired_by_agent = {}
        force_invoke_agents: set[uuid.UUID] = set()

    # Invoke each agent (with dedup window)
    for agent_id, agent_triggers in fired_by_agent.items():
        last = _last_invoke.get(agent_id)
        if agent_id not in force_invoke_agents and last and (now - last).total_seconds() < DEDUP_WINDOW:
            continue  # Skip - invoked too recently
        _last_invoke[agent_id] = now

        # ── Immediately update trigger state BEFORE launching async task ──
        # This prevents the next tick from re-evaluating the same trigger as
        # "should fire" while the LLM call is still running (which can take
        # minutes). Without this, the 15s tick interval + 30s dedup window
        # would cause repeated invocations for long-running triggers.
        try:
            for t in agent_triggers:
                cfg = mapping_from_row(t.config)
                if isinstance(cfg, str):
                    try:
                        cfg = mapping_from_row(json_loads_value(cfg))
                    except ValueError, TypeError:
                        cfg = {}
                if cfg.get("_execution_id"):
                    continue
                trigger = await agent_trigger_dao.get(t.id)
                if trigger:
                    fire_count = trigger.fire_count + 1
                    updates: dict[str, Any] = {
                        "last_fired_at": now,
                        "fire_count": fire_count,
                    }
                    if trigger.type == "once":
                        updates["is_enabled"] = False
                    if trigger.max_fires and fire_count >= trigger.max_fires:
                        updates["is_enabled"] = False
                    _ = await agent_trigger_dao.update(db_obj=trigger, obj_in=updates)
        except Exception as e:
            logger.warning(f"Failed to pre-update trigger state: {e}")

        task = asyncio.create_task(_invoke_agent_for_triggers(agent_id, agent_triggers))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_observe_background_task)


async def wake_agent_with_context(
    agent_id: uuid.UUID,
    message_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    skip_dedup: bool = False,
    a2a_session_id: str | None = None,
) -> None:
    """Public API: wake an agent asynchronously with a message context.

    Creates a synthetic trigger invocation so the agent processes the
    message in a Reflection Session via the standard trigger path.
    If a2a_session_id is provided, the agent's reply will also be saved
    to the A2A chat session for visibility in the admin chat history.
    Safe to call from any async context.

    Args:
        agent_id: The agent to wake.
        message_context: The message to deliver.
        from_agent_id: The agent that initiated this wake (for chain depth tracking).
        skip_dedup: If True, bypass the dedup window check.
        a2a_session_id: Optional A2A chat session ID to mirror the reply into.
    """

    now = datetime.now(UTC)

    if from_agent_id:
        chain_key = f"{from_agent_id}->{agent_id}"
        current_depth = _A2A_WAKE_CHAIN.get(chain_key, 0)
        if current_depth >= _A2A_MAX_WAKE_DEPTH:
            logger.warning(
                f"[A2A] Wake chain depth {current_depth} reached for {chain_key}, stopping to prevent wake storm"
            )
            return

        _A2A_WAKE_CHAIN[chain_key] = current_depth + 1

        def _decay_chain():
            _ = _A2A_WAKE_CHAIN.pop(chain_key, None)

        _ = asyncio.get_running_loop().call_later(_A2A_WAKE_CHAIN_TTL, _decay_chain)

    if not skip_dedup and agent_id in _last_invoke:
        elapsed = (now - _last_invoke[agent_id]).total_seconds()
        if elapsed < DEDUP_WINDOW:
            logger.info(
                f"[A2A] Skipping wake for agent {agent_id} - invoked {elapsed:.0f}s ago (dedup window {DEDUP_WINDOW}s)"
            )
            return

    _last_invoke[agent_id] = now

    from_agent_name = ""
    if from_agent_id:
        try:
            sender = await agent_dao.get(from_agent_id)
            from_agent_name = (sender.name if sender else "") or ""
        except Exception as e:
            logger.warning(f"Failed to lookup sender agent name: {e}")

    dummy_trigger = AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="a2a_wake",
        type="on_message",
        config={
            "from_agent_name": from_agent_name,
            "_matched_message": message_context[:2000],
            "_matched_from": "agent",
            "_a2a_session_id": a2a_session_id,
        },
        reason=(
            "You received a notification from another agent. "
            + "Read the message content above, update your focus and memory if needed, "
            + "and take any action you deem necessary. "
            + "Do NOT reply back to the sender unless you have a genuine question - "
            + "this was a notification, not a request for response."
        ),
        is_enabled=True,
        last_fired_at=now,
        fire_count=0,
    )
    task = asyncio.create_task(_invoke_agent_for_triggers(agent_id, [dummy_trigger]))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_observe_background_task)


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    logger.info("⚡ Trigger Daemon started (15s tick, heartbeat every ~60s)")
    _heartbeat_counter = 0
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.error(f"Trigger Daemon error: {e}")
            import traceback

            traceback.print_exc()

        # Run heartbeat check every 4th tick (~60 seconds)
        _heartbeat_counter += 1
        if _heartbeat_counter >= 4:
            _heartbeat_counter = 0
            _cleanup_stale_invoke_cache()
            try:
                from app.services.heartbeat import _heartbeat_tick

                await _heartbeat_tick()
            except Exception as e:
                logger.error(f"Heartbeat tick error: {e}")

        await asyncio.sleep(TICK_INTERVAL)
