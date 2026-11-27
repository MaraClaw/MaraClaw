"""Trigger daemon orchestrator.

Trigger-specific evaluation and invocation behavior now lives under
`app.services.trigger_runtime`. This module owns the main loop, dedup window,
and distributed claim/invoke flow.
"""

import asyncio
import contextvars
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import partial

from app.config import get_settings
from app.core.json_types import json_loads_value, mapping_from_row
from app.core.logging import logger, new_trace_id
from app.dao.agent_dao import agent_dao
from app.dao.trigger_dao import agent_trigger_dao
from app.records.trigger import AgentTriggerRecord, DueTrigger
from app.runtime.tasks import AdmissionClosedError, TaskAdmission, loop_progress
from app.services.trigger_runtime import (
    claim_ready_trigger_invocations,
    enqueue_due_trigger,
)
from app.services.trigger_runtime.evaluator import (
    evaluate_trigger as evaluate_trigger_runtime,
    mark_trigger_skipped as mark_trigger_skipped_runtime,
    should_skip_non_workday as should_skip_non_workday_runtime,
)
from app.services.trigger_runtime.invoker import invoke_agent_for_triggers as invoke_agent_for_triggers_runtime
from app.services.trigger_runtime.scan import Cursor, TriggerScanner

TICK_INTERVAL = 15  # seconds
DEDUP_WINDOW = 30  # seconds - same agent won't be invoked twice within this window
MIN_POLL_INTERVAL_MINUTES = 5  # minimum poll interval to prevent abuse
EVALUATION_SETTLE_SECONDS = 5.0  # how long a tick waits for evaluations before it claims; slow ones finish on their own
# An evaluation (a poll fetch is bounded by a 10-second client timeout) gives up its scanner slot after this long.
# Only the evaluation: the enqueue writes state (a poll baseline in its transaction), so it is never cancelled.
EVALUATION_DEADLINE_SECONDS = 60.0
# Readiness fails when no tick finished this long; a tick is TICK_INTERVAL plus the settle window and the claim phase.
PROGRESS_BUDGET_SECONDS = 300.0

# Safety: per-agent on_message fire rate limiter
_ON_MSG_RATE_WINDOW = 3600  # 1 hour window
_ON_MSG_RATE_LIMIT = 30  # max on_message fires per agent per hour
_on_msg_fire_log: dict[uuid.UUID, list[datetime]] = {}  # agent_id -> list of fire timestamps

_last_invoke: dict[uuid.UUID, datetime] = {}

_A2A_WAKE_CHAIN: dict[str, int] = {}
_A2A_WAKE_CHAIN_TTL = 300
_A2A_MAX_WAKE_DEPTH = 3


class InvocationCapacity:
    """Invocation slots of one lifespan: slots held by reservations and running invocations never exceed the limit.

    Counts change only synchronously, so a reservation is settled before its holder next awaits. A claim reserves
    every free slot before it awaits the database; a wake that finds none meanwhile waits until the claim keeps what
    it leased, so it is refused only when the process is still full, never for slots the claim hands back.
    """

    def __init__(self, limit: int) -> None:
        self.limit: int = limit
        self.held: int = 0
        self.unsettled_claims: set[asyncio.Event] = set()

    @property
    def free(self) -> int:
        return self.limit - self.held

    def _reserve(self, wanted: int, settled: asyncio.Event | None = None) -> SlotReservation:
        slots = min(wanted, self.free)
        self.held += slots
        return SlotReservation(self, slots, settled)

    def reserve_for_claim(self) -> SlotReservation:
        """Hold every free slot, possibly none, until the claim keeps the slots of the agents it leased."""
        if not self.free:
            return self._reserve(0)
        settled = asyncio.Event()
        self.unsettled_claims.add(settled)
        return self._reserve(self.free, settled)

    async def reserve_for_wake(self) -> SlotReservation:
        """Hold one slot, or none when the process is full after every claim in flight has settled."""
        while not self.free and self.unsettled_claims:
            _ = await next(iter(self.unsettled_claims)).wait()
        return self._reserve(1)


class SlotReservation:
    """Slots held from an ``InvocationCapacity`` until invocations take them or they are released."""

    def __init__(self, capacity: InvocationCapacity, slots: int, settled: asyncio.Event | None = None) -> None:
        self._capacity: InvocationCapacity = capacity
        self.slots: int = slots
        self._settled: asyncio.Event | None = settled

    def take(self) -> SlotReservation:
        """Move one slot into a reservation of its own, which the invocation releases when it ends."""
        if not self.slots:
            raise RuntimeError("No reserved trigger invocation slot left")
        self.slots -= 1
        return SlotReservation(self._capacity, 1)

    def release(self, *, keep: int = 0) -> None:
        """Return every slot beyond ``keep`` and settle a claim; a slot is never returned twice."""
        returned = max(0, self.slots - keep)
        self.slots -= returned
        self._capacity.held -= returned
        if self._settled is not None:
            self._capacity.unsettled_claims.discard(self._settled)
            self._settled.set()


_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
_admission = TaskAdmission("Trigger invocation", _BACKGROUND_TASKS)
_capacity = InvocationCapacity(get_settings().TRIGGER_MAX_CONCURRENT_INVOCATIONS)
_scanner = TriggerScanner()


def open_trigger_admission() -> None:
    """Reopen invocation admission for a new lifespan, with every invocation slot free."""
    global _capacity
    # Invocations an earlier lifespan left behind release into its own ledger, never into this one.
    _capacity = InvocationCapacity(get_settings().TRIGGER_MAX_CONCURRENT_INVOCATIONS)
    _admission.open()


def close_trigger_admission() -> None:
    """Stop claiming and admitting invocations; admitted claims and wakes still run to completion."""
    _admission.close()


async def drain_trigger_tasks(grace_seconds: float = 20.0) -> None:
    """Wait for claims, wakes and invocations, then cancel and await the rest; cancelled leases recover by expiry."""
    await _admission.drain(grace_seconds)


def _observe_background_task(task: asyncio.Task[None]) -> None:
    _BACKGROUND_TASKS.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        logger.exception("Trigger invocation failed")


def _start_invocation(agent_id: uuid.UUID, triggers: list[AgentTriggerRecord], reservation: SlotReservation) -> None:
    # Only called from work admitted by `_admission.run`, which a drain joins before it snapshots invocations.
    # Callers may hold a connection_ctx or tool execution context.
    task = asyncio.create_task(_invoke_agent_for_triggers(agent_id, triggers), context=contextvars.Context())
    # Taken only once the task exists: a failed create_task leaves the slot in the reservation its caller releases.
    slot = reservation.take()
    _BACKGROUND_TASKS.add(task)
    # Completion, failure and cancellation all end in this callback, so the slot returns exactly once. A terminal
    # write whose commit is uncertain fails only the task, never the execution; lease recovery settles it.
    task.add_done_callback(lambda _task: slot.release())
    task.add_done_callback(_observe_background_task)


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


async def _evaluate_trigger(trigger: AgentTriggerRecord, now: datetime, agent_timezone: str | None = None) -> bool:
    return await evaluate_trigger_runtime(trigger, now, agent_timezone=agent_timezone)


async def _invoke_agent_for_triggers(agent_id: uuid.UUID, triggers: list[AgentTriggerRecord]):
    _ = new_trace_id()
    await invoke_agent_for_triggers_runtime(agent_id, triggers)


# ── Main Tick Loop ──────────────────────────────────────────────────


async def _evaluate_within_deadline(trigger: AgentTriggerRecord, now: datetime, agent_timezone: str | None) -> bool:
    """Whether the trigger is due; a hung evaluation is cancelled at the deadline and the trigger waits for its turn."""
    deadline = asyncio.timeout(EVALUATION_DEADLINE_SECONDS)
    try:
        async with deadline:
            return await _evaluate_trigger(trigger, now, agent_timezone)
    except TimeoutError:
        if not deadline.expired():
            raise
        logger.warning(f"Trigger '{trigger.name}' evaluation exceeded {EVALUATION_DEADLINE_SECONDS:g}s; abandoned")
        return False


async def _process_trigger(trigger: AgentTriggerRecord, now: datetime, agent_timezone: str | None) -> None:
    """Evaluate one trigger and enqueue it when due; the scanner isolates its failure from the other triggers."""
    _ = new_trace_id()
    # Auto-disable expired triggers
    if trigger.expires_at and now >= trigger.expires_at:
        t = await agent_trigger_dao.get(trigger.id)
        if t:
            _ = await agent_trigger_dao.update(db_obj=t, obj_in={"is_enabled": False})
        return

    if not await _evaluate_within_deadline(trigger, now, agent_timezone):
        return
    # Fix 3: Rate limit on_message triggers per agent
    if trigger.type == "on_message":
        agent_fires = _on_msg_fire_log.get(trigger.agent_id, [])
        cutoff = now - timedelta(seconds=_ON_MSG_RATE_WINDOW)
        recent = [t for t in agent_fires if t > cutoff]
        if len(recent) >= _ON_MSG_RATE_LIMIT:
            logger.warning(
                f"[A2A Safety] Agent {trigger.agent_id} hit "
                f"on_message rate limit ({_ON_MSG_RATE_LIMIT}/hr). "
                f"Auto-disabling trigger '{trigger.name}'."
            )
            t_obj = await agent_trigger_dao.get(trigger.id)
            if t_obj:
                _ = await agent_trigger_dao.update(db_obj=t_obj, obj_in={"is_enabled": False})
            return
        recent.append(now)
        _on_msg_fire_log[trigger.agent_id] = recent
    await enqueue_due_trigger(trigger, now, agent_timezone)


async def _tick():
    """One daemon tick: evaluate triggers concurrently, claim queued executions, group by agent, invoke."""
    _ = new_trace_id()
    now = datetime.now(UTC)

    # Evaluate and enqueue due triggers. Agent invocation happens only after
    # executions are claimed through the distributed execution queue.
    if _admission.accepting:

        async def read_page(after: Cursor | None, limit: int) -> Sequence[DueTrigger]:
            return await agent_trigger_dao.list_due_page(now=now, after=after, limit=limit)

        await _scanner.scan(
            read_page,
            lambda due: _process_trigger(due.trigger, now, due.agent_timezone),
            settle=EVALUATION_SETTLE_SECONDS,
        )

    await _claim_and_invoke(now)


async def _claim_and_invoke(now: datetime) -> None:
    # The claim and the launch of what it leased are one admitted unit: a drain that begins mid-claim waits for the
    # launch instead of refusing leased executions, and one that runs out of grace cancels the unit (lease recovery).
    try:
        await _admission.run(partial(_launch_claimed, now, _capacity))
    except AdmissionClosedError:
        logger.info("Trigger claim skipped: shutdown drain in progress")


async def _launch_claimed(now: datetime, capacity: InvocationCapacity) -> None:
    # Every free slot is reserved before the claim awaits the database, so a wake arriving meanwhile cannot take a
    # slot the claimed work needs. A full process claims nothing but still recovers abandoned executions.
    reservation = capacity.reserve_for_claim()
    try:
        await _launch_reserved(now, reservation)
    finally:
        reservation.release()


async def _launch_reserved(now: datetime, reservation: SlotReservation) -> None:
    # Claim queued executions with a DB lease so only one worker handles each event. A failed or uncertain claim
    # commit launches nothing and marks nothing failed: any lease it did commit is recovered when it expires.
    try:
        fired_by_agent, force_invoke_agents = await claim_ready_trigger_invocations(now, max_agents=reservation.slots)
    except Exception as e:
        logger.warning(f"Failed to claim trigger executions: {e}")
        return
    # One slot per leased agent; the rest go back at once to waiting wakes and the next tick.
    reservation.release(keep=len(fired_by_agent))

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
                    updates: dict[str, datetime | int | bool] = {
                        "last_fired_at": now,
                        "fire_count": fire_count,
                    }
                    if trigger.type == "once":
                        updates["is_enabled"] = False
                    if trigger.max_fires not in (None, 0) and fire_count >= trigger.max_fires:
                        updates["is_enabled"] = False
                    _ = await agent_trigger_dao.update(db_obj=trigger, obj_in=updates)
        except Exception as e:
            logger.warning(f"Failed to pre-update trigger state: {e}")

        _start_invocation(agent_id, agent_triggers, reservation)


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
    capacity = _capacity

    async def admitted() -> None:
        # A synthetic wake has no durable queue: without a free slot it is refused before any guard is consumed.
        reservation = await capacity.reserve_for_wake()
        if not reservation.slots:
            logger.warning(
                "[A2A] Wake for agent {} refused: all {} trigger invocation slots are busy", agent_id, capacity.limit
            )
            return
        try:
            await launch(reservation)
        finally:
            reservation.release()

    async def launch(reservation: SlotReservation) -> None:
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
                "Read the message content above, update your focus and memory if needed, "
                "and take any action you deem necessary. "
                "Do NOT reply back to the sender unless you have a genuine question - "
                "this was a notification, not a request for response."
            ),
            is_enabled=True,
            last_fired_at=now,
            fire_count=0,
        )
        _start_invocation(agent_id, [dummy_trigger], reservation)

    # Admission and capacity are checked before the chain or dedup guards are consumed, so a refused wake leaves both
    # untouched. The sender lookup runs inside the admitted unit holding its slot, so neither a drain nor a claim can
    # refuse a wake that already consumed them.
    try:
        await _admission.run(admitted)
    except AdmissionClosedError:
        logger.info("[A2A] Wake for agent {} skipped: shutdown drain in progress", agent_id)


async def start_trigger_daemon():
    """Start the background trigger daemon loop. Called from FastAPI startup."""
    logger.info("⚡ Trigger Daemon started (15s tick)")
    progress = loop_progress()
    _cleanup_counter = 0
    try:
        while True:
            try:
                await _tick()
            except Exception as e:
                logger.error(f"Trigger Daemon error: {e}")
                import traceback

                traceback.print_exc()
            else:
                progress.beat()

            # Prune the dedup and rate-limit caches every 4th tick (~60 seconds)
            _cleanup_counter += 1
            if _cleanup_counter >= 4:
                _cleanup_counter = 0
                _cleanup_stale_invoke_cache()

            await asyncio.sleep(TICK_INTERVAL)
    finally:
        await _scanner.close()
