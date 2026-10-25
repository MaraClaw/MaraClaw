import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.records.trigger import AgentTriggerRecord
from app.services import trigger_daemon
from app.services.trigger_runtime import invoker


@pytest.fixture(autouse=True)
def isolated_daemon(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def no_triggers() -> list[AgentTriggerRecord]:
        return []

    monkeypatch.setattr(trigger_daemon.agent_trigger_dao, "list_enabled", no_triggers)
    monkeypatch.setattr(trigger_daemon, "_last_invoke", {})
    monkeypatch.setattr(trigger_daemon, "_A2A_WAKE_CHAIN", {})
    trigger_daemon.open_trigger_admission()
    try:
        yield
    finally:
        trigger_daemon.open_trigger_admission()


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
