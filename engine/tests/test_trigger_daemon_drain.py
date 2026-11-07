import asyncio
import contextvars
import itertools
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.records.trigger import AgentTriggerRecord
from app.services import heartbeat, trigger_daemon
from app.services.trigger_runtime import invoker, scan
from app.services.trigger_runtime.scan import TriggerScanner


@pytest.fixture(autouse=True)
def isolated_daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def no_triggers(*, after: uuid.UUID | None, limit: int) -> list[AgentTriggerRecord]:
        return []

    monkeypatch.setattr(trigger_daemon.agent_trigger_dao, "list_enabled_page", no_triggers)
    monkeypatch.setattr(trigger_daemon, "_last_invoke", {})
    monkeypatch.setattr(trigger_daemon, "_A2A_WAKE_CHAIN", {})
    trigger_daemon.open_trigger_admission()
    try:
        yield
    finally:
        trigger_daemon.open_trigger_admission()


@pytest.fixture(autouse=True)
async def _close_scanner() -> AsyncIterator[None]:
    try:
        yield
    finally:
        await trigger_daemon._scanner.close()


def claimed_trigger(agent_id: uuid.UUID) -> AgentTriggerRecord:
    return AgentTriggerRecord(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="claimed",
        type="interval",
        config={"_execution_id": str(uuid.uuid4())},
        reason="due",
        is_enabled=True,
        last_fired_at=datetime.now(UTC),
        fire_count=1,
    )


def patch_claim(
    monkeypatch: pytest.MonkeyPatch, agent_id: uuid.UUID, claim: Callable[[], Awaitable[None]]
) -> list[AgentTriggerRecord]:
    claimed = [claimed_trigger(agent_id)]

    async def claim_ready(_now: datetime) -> tuple[dict[uuid.UUID, list[AgentTriggerRecord]], set[uuid.UUID]]:
        await claim()
        return {agent_id: claimed}, {agent_id}

    monkeypatch.setattr(trigger_daemon, "claim_ready_trigger_invocations", claim_ready)
    return claimed


async def test_tick_does_not_claim_once_admission_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a closed gate
    async def claim() -> None:
        pytest.fail("a closed gate must not lease executions")

    patch_claim(monkeypatch, uuid.uuid4(), claim)
    trigger_daemon.close_trigger_admission()
    # When / Then
    await trigger_daemon._tick()
    assert not trigger_daemon._BACKGROUND_TASKS


async def test_claim_in_flight_when_gate_closes_still_launches_and_is_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a tick suspended inside the claim await
    agent_id = uuid.uuid4()
    in_claim = asyncio.Event()
    finish_claim = asyncio.Event()
    invoked: list[list[AgentTriggerRecord]] = []

    async def claim() -> None:
        in_claim.set()
        await finish_claim.wait()

    async def invoke(_agent_id: uuid.UUID, triggers: list[AgentTriggerRecord]) -> None:
        await asyncio.sleep(0)
        invoked.append(triggers)

    claimed = patch_claim(monkeypatch, agent_id, claim)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    tick = asyncio.create_task(trigger_daemon._tick())
    await in_claim.wait()
    # When shutdown closes admission and starts draining before the claim returns
    trigger_daemon.close_trigger_admission()
    drain = asyncio.create_task(trigger_daemon.drain_trigger_tasks(grace_seconds=1))
    await asyncio.sleep(0)
    assert not drain.done()
    finish_claim.set()
    await tick
    await drain
    # Then the leased executions were launched, not refused, and the drain joined them
    assert invoked == [claimed]
    assert not trigger_daemon._BACKGROUND_TASKS


async def test_invocation_finishing_within_grace_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a running invocation that ends shortly after the drain begins
    agent_id = uuid.uuid4()
    release = asyncio.Event()
    outcome: list[str] = []

    async def invoke(_agent_id: uuid.UUID, _triggers: list[AgentTriggerRecord]) -> None:
        try:
            await release.wait()
            outcome.append("completed")
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    async def claim() -> None:
        return None

    patch_claim(monkeypatch, agent_id, claim)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    await trigger_daemon._tick()
    trigger_daemon.close_trigger_admission()
    drain = asyncio.create_task(trigger_daemon.drain_trigger_tasks(grace_seconds=1))
    await asyncio.sleep(0.01)
    # When it finishes inside the grace period
    release.set()
    await drain
    # Then
    assert outcome == ["completed"]
    assert not trigger_daemon._BACKGROUND_TASKS


async def test_invocation_cancelled_after_grace_goes_to_lease_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the real invoker holding leased executions that never finish, and a task owned by someone else
    agent_id = uuid.uuid4()
    started = asyncio.Event()
    settled: list[str] = []

    @asynccontextmanager
    async def hold(_leases: object) -> AsyncGenerator[None]:
        yield

    async def run_forever(*_args: object) -> str | None:
        started.set()
        await asyncio.Event().wait()

    async def mark_failed(*_args: object) -> list[uuid.UUID]:
        settled.append("failed")
        return []

    async def mark_completed(*_args: object) -> list[uuid.UUID]:
        settled.append("completed")
        return []

    monkeypatch.setattr(invoker, "trigger_execution_leases", lambda _triggers: [])
    monkeypatch.setattr(invoker, "hold_trigger_execution_leases", hold)
    monkeypatch.setattr(invoker, "_run_trigger_invocation", run_forever)
    monkeypatch.setattr(invoker, "mark_trigger_executions_failed", mark_failed)
    monkeypatch.setattr(invoker, "mark_trigger_executions_completed", mark_completed)

    async def claim() -> None:
        return None

    patch_claim(monkeypatch, agent_id, claim)
    foreign = asyncio.create_task(asyncio.Event().wait())
    await trigger_daemon._tick()
    await started.wait()
    trigger_daemon.close_trigger_admission()
    # When the grace period expires
    await trigger_daemon.drain_trigger_tasks(grace_seconds=0.01)
    # Then the invocation was cancelled without any terminal status and unrelated tasks were left alone
    assert settled == []
    assert not trigger_daemon._BACKGROUND_TASKS
    assert not foreign.done()
    foreign.cancel()
    _ = await asyncio.gather(foreign, return_exceptions=True)


async def test_wake_refused_after_gate_closes_leaves_guards_untouched() -> None:
    # Given a closed gate
    agent_id, sender = uuid.uuid4(), uuid.uuid4()
    trigger_daemon.close_trigger_admission()
    # When
    await trigger_daemon.wake_agent_with_context(agent_id, "hello", from_agent_id=sender)
    # Then neither dedup nor wake-chain budget was consumed and nothing started
    assert trigger_daemon._last_invoke == {}
    assert trigger_daemon._A2A_WAKE_CHAIN == {}
    assert not trigger_daemon._BACKGROUND_TASKS


async def test_wake_in_flight_when_gate_closes_still_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a wake suspended in its sender lookup after consuming dedup and chain
    agent_id, sender = uuid.uuid4(), uuid.uuid4()
    in_lookup = asyncio.Event()
    finish_lookup = asyncio.Event()
    invoked: list[list[AgentTriggerRecord]] = []

    async def lookup(_agent_id: uuid.UUID) -> SimpleNamespace:
        in_lookup.set()
        await finish_lookup.wait()
        return SimpleNamespace(name="Sender")

    async def invoke(_agent_id: uuid.UUID, triggers: list[AgentTriggerRecord]) -> None:
        invoked.append(triggers)

    monkeypatch.setattr(trigger_daemon.agent_dao, "get", lookup)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    wake = asyncio.create_task(trigger_daemon.wake_agent_with_context(agent_id, "hello", from_agent_id=sender))
    await in_lookup.wait()
    # When the gate closes and a drain starts mid-lookup
    trigger_daemon.close_trigger_admission()
    drain = asyncio.create_task(trigger_daemon.drain_trigger_tasks(grace_seconds=1))
    await asyncio.sleep(0)
    finish_lookup.set()
    await wake
    await drain
    # Then the consumed wake was not refused
    assert [triggers[0].config["from_agent_name"] for triggers in invoked] == ["Sender"]
    assert agent_id in trigger_daemon._last_invoke


async def test_claim_outliving_grace_is_cancelled_and_joined_before_drain_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a claim that outlives the whole grace period and releases something in a finalizer
    agent_id = uuid.uuid4()
    in_claim = asyncio.Event()
    finalized = asyncio.Event()
    never = asyncio.Event()

    async def claim() -> None:
        in_claim.set()
        try:
            await never.wait()
        finally:
            finalized.set()

    async def invoke(*_args: object) -> None:
        pytest.fail("work cancelled by the drain must not launch")

    patch_claim(monkeypatch, agent_id, claim)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    tick = asyncio.create_task(trigger_daemon._tick())
    await in_claim.wait()
    # When the drain gives up on it
    await trigger_daemon.drain_trigger_tasks(grace_seconds=0.01)
    # Then the claim was cancelled and finalized before the drain returned, so nothing runs against a closed pool
    assert finalized.is_set()
    await tick
    never.set()
    await asyncio.sleep(0)
    assert not trigger_daemon._BACKGROUND_TASKS


async def test_wake_outliving_grace_is_cancelled_before_drain_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a wake stuck in its sender lookup after consuming the guards
    agent_id, sender = uuid.uuid4(), uuid.uuid4()
    in_lookup = asyncio.Event()
    finalized = asyncio.Event()

    async def lookup(_agent_id: uuid.UUID) -> SimpleNamespace:
        in_lookup.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        return SimpleNamespace(name="Sender")

    async def invoke(*_args: object) -> None:
        pytest.fail("a wake cancelled by the drain must not launch")

    monkeypatch.setattr(trigger_daemon.agent_dao, "get", lookup)
    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    wake = asyncio.create_task(trigger_daemon.wake_agent_with_context(agent_id, "hello", from_agent_id=sender))
    await in_lookup.wait()
    # When the drain runs out of grace
    await trigger_daemon.drain_trigger_tasks(grace_seconds=0.01)
    # Then the lookup was cancelled first, and the caller returns without a launch
    assert finalized.is_set()
    await wake
    assert not trigger_daemon._BACKGROUND_TASKS


async def test_reopened_gate_admits_again(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a gate closed by an earlier lifespan
    started = asyncio.Event()

    async def invoke(*_args: object) -> None:
        started.set()

    monkeypatch.setattr(trigger_daemon, "_invoke_agent_for_triggers", invoke)
    trigger_daemon.close_trigger_admission()
    # When the next lifespan reopens it
    trigger_daemon.open_trigger_admission()
    await trigger_daemon.wake_agent_with_context(uuid.uuid4(), "hello", skip_dedup=True)
    await asyncio.wait_for(started.wait(), 1)
    await trigger_daemon.drain_trigger_tasks(grace_seconds=1)


_scan_ids = itertools.count(1)


def scan_trigger(name: str, trigger_type: str = "poll", **config: object) -> AgentTriggerRecord:
    # Ids ascend in creation order, like the id-ordered pages the daemon reads.
    return AgentTriggerRecord(
        id=uuid.UUID(int=next(_scan_ids)),
        agent_id=uuid.uuid4(),
        name=name,
        type=trigger_type,
        config=dict(config),
        is_enabled=True,
    )


def trigger_pages(triggers: list[AgentTriggerRecord]) -> Callable[..., Awaitable[list[AgentTriggerRecord]]]:
    ordered = sorted(triggers, key=lambda trigger: trigger.id)

    async def page(*, after: uuid.UUID | None, limit: int) -> list[AgentTriggerRecord]:
        return [trigger for trigger in ordered if after is None or trigger.id > after][:limit]

    return page


def patch_scan(
    monkeypatch: pytest.MonkeyPatch,
    triggers: list[AgentTriggerRecord],
    evaluate: Callable[[AgentTriggerRecord, datetime, str | None], Awaitable[bool]],
) -> list[datetime]:
    claims: list[datetime] = []

    async def claim_ready(now: datetime) -> tuple[dict[uuid.UUID, list[AgentTriggerRecord]], set[uuid.UUID]]:
        claims.append(now)
        return {}, set()

    monkeypatch.setattr(trigger_daemon.agent_trigger_dao, "list_enabled_page", trigger_pages(triggers))
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", evaluate)
    monkeypatch.setattr(trigger_daemon, "claim_ready_trigger_invocations", claim_ready)
    return claims


async def test_slow_evaluation_does_not_delay_the_claim_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given one trigger whose poll never returns and one that finishes at once
    slow, fast = scan_trigger("slow"), scan_trigger("fast")
    release, slow_done = asyncio.Event(), asyncio.Event()
    started: list[str] = []
    finished: list[str] = []

    async def evaluate(trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        started.append(trigger.name)
        if trigger is slow:
            await release.wait()
            slow_done.set()
        finished.append(trigger.name)
        return False

    claims = patch_scan(monkeypatch, [slow, fast], evaluate)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0.01)
    # When a tick runs
    await trigger_daemon._tick()
    # Then the claim phase ran while the slow evaluation is still pending, and the fast one was not held up
    assert len(claims) == 1
    assert finished == ["fast"]
    # And the next tick leaves the unfinished trigger alone instead of polling it again
    await trigger_daemon._tick()
    assert started == ["slow", "fast", "fast"]
    release.set()
    await asyncio.wait_for(slow_done.wait(), 1)
    assert len(claims) == 2


async def test_one_failing_trigger_does_not_cancel_the_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a trigger whose evaluation raises between two healthy ones
    triggers = [scan_trigger("first"), scan_trigger("broken"), scan_trigger("last")]
    evaluated: list[str] = []

    async def evaluate(trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        if trigger.name == "broken":
            raise RuntimeError("boom")
        evaluated.append(trigger.name)
        return False

    claims = patch_scan(monkeypatch, triggers, evaluate)
    # When a tick runs
    await trigger_daemon._tick()
    # Then the others finished and the claim phase still ran
    assert sorted(evaluated) == ["first", "last"]
    assert len(claims) == 1


async def test_concurrent_evaluations_stay_under_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given six triggers and a scanner bounded at two
    triggers = [scan_trigger(f"t{i}") for i in range(6)]
    scanner = TriggerScanner(limit=2)
    monkeypatch.setattr(trigger_daemon, "_scanner", scanner)
    release = asyncio.Event()
    both_started = asyncio.Event()
    active = peak = 0
    evaluated: list[str] = []

    async def evaluate(trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        try:
            await release.wait()
        finally:
            active -= 1
            evaluated.append(trigger.name)
        return False

    _ = patch_scan(monkeypatch, triggers, evaluate)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    # When a tick leaves two pending
    await trigger_daemon._tick()
    await asyncio.wait_for(both_started.wait(), 1)
    # Then only those two exist, not one task per trigger
    assert active == peak == 2
    assert len(scanner._tasks) == 2
    # And once released, the next tick picks up the four that were deferred
    release.set()
    _ = await asyncio.gather(*scanner._tasks.values())
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 5)
    await trigger_daemon._tick()
    assert sorted(evaluated) == [f"t{i}" for i in range(6)]
    assert peak == 2
    await scanner.close()


async def test_evaluations_start_in_an_empty_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a tick running under a caller context
    ambient: contextvars.ContextVar[str | None] = contextvars.ContextVar("ambient", default=None)
    seen: list[str | None] = []

    async def evaluate(_trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        seen.append(ambient.get())
        return False

    _ = patch_scan(monkeypatch, [scan_trigger("a")], evaluate)
    _ = ambient.set("caller connection")
    # When / Then the evaluation does not inherit it
    await trigger_daemon._tick()
    assert seen == [None]


async def test_cron_timezones_are_resolved_once_for_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given cron triggers on three agents (two share a company), one with its own timezone, and a poll trigger
    tenant_id = uuid.uuid4()
    first, second, third = scan_trigger("a", "cron"), scan_trigger("b", "cron"), scan_trigger("c", "cron")
    second.agent_id = first.agent_id
    configured = scan_trigger("d", "cron", timezone="Asia/Tokyo")
    polled = scan_trigger("e")
    agents = [
        SimpleNamespace(id=first.agent_id, tenant_id=tenant_id, timezone=None),
        SimpleNamespace(id=third.agent_id, tenant_id=None, timezone="Europe/Paris"),
    ]
    lookups: list[str] = []

    async def get_many(ids: list[uuid.UUID]) -> list[SimpleNamespace]:
        lookups.append("agents")
        return [agent for agent in agents if agent.id in ids]

    async def get_by_ids(ids: list[uuid.UUID]) -> list[SimpleNamespace]:
        lookups.append("tenants")
        assert ids == [tenant_id]
        return [SimpleNamespace(id=tenant_id, timezone="Asia/Seoul")]

    async def per_trigger_lookup(*_args: object) -> object:
        pytest.fail("a cron evaluation must not read its agent or tenant itself")

    monkeypatch.setattr(scan.agent_dao, "get_many", get_many)
    monkeypatch.setattr(scan.tenant_dao, "get_by_ids", get_by_ids)
    monkeypatch.setattr(scan.agent_dao, "get", per_trigger_lookup)
    monkeypatch.setattr(scan.tenant_dao, "get", per_trigger_lookup)
    zones: dict[str, str | None] = {}

    async def evaluate(trigger: AgentTriggerRecord, _now: datetime, zone: str | None = None) -> bool:
        zones[trigger.name] = zone
        return False

    _ = patch_scan(monkeypatch, [first, second, third, configured, polled], evaluate)
    # When a tick evaluates them
    await trigger_daemon._tick()
    # Then two queries served every trigger, and each cron trigger got its agent's timezone
    assert lookups == ["agents", "tenants"]
    assert zones == {"a": "Asia/Seoul", "b": "Asia/Seoul", "c": "Europe/Paris", "d": None, "e": None}


async def test_tick_without_cron_triggers_reads_no_timezones(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*_args: object) -> object:
        pytest.fail("no cron trigger, no timezone read")

    monkeypatch.setattr(scan.agent_dao, "get_many", forbidden)

    async def evaluate(_trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        return False

    _ = patch_scan(monkeypatch, [scan_trigger("poll")], evaluate)
    await trigger_daemon._tick()


async def test_timezone_batch_failure_does_not_cause_per_trigger_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    triggers = [scan_trigger("a", "cron"), scan_trigger("b", "cron")]
    zones: list[str | None] = []

    async def fail_batch(_triggers: object) -> dict[uuid.UUID, str]:
        raise RuntimeError("database unavailable")

    async def evaluate(_trigger: AgentTriggerRecord, _now: datetime, zone: str | None = None) -> bool:
        zones.append(zone)
        return False

    monkeypatch.setattr(trigger_daemon, "cron_agent_timezones", fail_batch)
    _ = patch_scan(monkeypatch, triggers, evaluate)
    await trigger_daemon._tick()
    assert zones == ["UTC", "UTC"]


async def test_tick_skips_evaluation_once_admission_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def evaluate(*_args: object) -> bool:
        pytest.fail("a closed gate must not start evaluations")

    claims = patch_scan(monkeypatch, [scan_trigger("poll")], evaluate)
    trigger_daemon.close_trigger_admission()
    await trigger_daemon._tick()
    assert claims == []


async def test_scanner_close_cancels_and_joins_pending_evaluations(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an evaluation that never returns
    stopped = asyncio.Event()
    started = asyncio.Event()

    async def evaluate(_trigger: AgentTriggerRecord, _now: datetime, _zone: str | None = None) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return False

    _ = patch_scan(monkeypatch, [scan_trigger("stuck")], evaluate)
    monkeypatch.setattr(trigger_daemon, "EVALUATION_SETTLE_SECONDS", 0)
    await trigger_daemon._tick()
    await started.wait()
    # When the scanner closes
    await trigger_daemon._scanner.close()
    # Then the evaluation was cancelled and finished before close returned
    assert stopped.is_set()


async def test_daemon_loop_closes_the_scanner_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a running daemon loop
    closed = asyncio.Event()
    ticking = asyncio.Event()

    class _Scanner:
        async def close(self) -> None:
            closed.set()

    async def tick() -> None:
        ticking.set()

    monkeypatch.setattr(trigger_daemon, "_scanner", _Scanner())
    monkeypatch.setattr(trigger_daemon, "_tick", tick)
    monkeypatch.setattr(trigger_daemon, "TICK_INTERVAL", 3600)
    loop = asyncio.create_task(trigger_daemon.start_trigger_daemon())
    await ticking.wait()
    # When it is cancelled
    loop.cancel()
    _ = await asyncio.gather(loop, return_exceptions=True)
    # Then the scanner was closed
    assert closed.is_set()


async def test_daemon_loop_prunes_caches_every_fourth_tick_and_leaves_heartbeats_to_their_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a daemon loop with instant ticks, a heartbeat that must not be called from it, and a ninth tick that parks
    ticks = pruned = 0
    parked = asyncio.Event()

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 9:
            parked.set()
            await asyncio.Event().wait()

    def prune() -> None:
        nonlocal pruned
        pruned += 1

    async def heartbeat_tick() -> None:
        pytest.fail("the trigger daemon must not run heartbeats")

    monkeypatch.setattr(trigger_daemon, "_tick", tick)
    monkeypatch.setattr(trigger_daemon, "_cleanup_stale_invoke_cache", prune)
    monkeypatch.setattr(trigger_daemon, "TICK_INTERVAL", 0)
    monkeypatch.setattr(heartbeat, "_heartbeat_tick", heartbeat_tick)
    # When it runs through nine ticks
    loop = asyncio.create_task(trigger_daemon.start_trigger_daemon())
    await asyncio.wait_for(parked.wait(), 1)
    loop.cancel()
    _ = await asyncio.gather(loop, return_exceptions=True)
    # Then caches were pruned after the fourth and eighth
    assert pruned == 2
