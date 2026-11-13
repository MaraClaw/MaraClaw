import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.test_background_llm import BackgroundClient

from app.records.agent import AgentRecord
from app.services import heartbeat
from app.services.llm.types import LLMResponse, LLMToolCall, ToolPayload
from test_finish_protocol import _finish_response, _model
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]

BUDGET = heartbeat.PROGRESS_BUDGET_SECONDS


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_tool_argument_preview_formats_mapping_arguments() -> None:
    # Given: a tool call with already-parsed mapping arguments.
    arguments: ToolPayload = {"recipient": "ops"}

    # When: heartbeat prepares the arguments for a bounded log preview.
    preview = heartbeat._tool_argument_preview(arguments, 20)

    # Then: the mapping is represented without slice-based failure.
    assert preview == "{'recipient': 'ops'}"


def test_tool_call_parts_rejects_missing_function() -> None:
    # Given: a malformed provider tool call with no function payload.
    tool_call: LLMToolCall = {"id": "call-1"}

    # When: heartbeat prepares the tool call for execution.
    # Then: it rejects the malformed call before execution.
    with pytest.raises(KeyError, match="function"):
        heartbeat._tool_call_parts(tool_call)


@pytest.mark.asyncio
async def test_run_agent_oneshot_logs_zero_completed_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an agent and client that reach the loop with no allowed rounds.
    agent_id = uuid.uuid4()
    model_id = uuid.uuid4()
    fake_agent = SimpleNamespace(
        primary_model_id=model_id,
        fallback_model_id=None,
        name="Heartbeat Agent",
        role_description=None,
        creator_id=None,
    )
    fake_model = SimpleNamespace(
        provider="openai",
        model="test-model",
        base_url=None,
        temperature=None,
        max_output_tokens=None,
        request_timeout=None,
    )
    client = _Client()
    info_messages: list[str] = []
    exception_messages: list[str] = []

    async def get_agent(_id: object) -> object:
        return fake_agent

    async def get_model(_id: object) -> object:
        return fake_model

    async def build_context(*args: object) -> tuple[str, str]:
        return "system", "dynamic"

    async def get_tools(*args: object) -> list[dict[str, object]]:
        return []

    def record_info(message: str) -> None:
        info_messages.append(message)

    def record_exception(message: str) -> None:
        exception_messages.append(message)

    from app.dao.agent_dao import agent_dao
    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(agent_dao, "get", get_agent)
    monkeypatch.setattr(llm_model_dao, "get", get_model)
    monkeypatch.setattr("app.services.agent_context.build_agent_context", build_context)
    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", get_tools)
    monkeypatch.setattr("app.services.llm.get_model_api_key", lambda model: "key")

    async def _fresh_client(model, **_kwargs):
        return model, client

    monkeypatch.setattr("app.services.llm.create_fresh_llm_client_from_model", _fresh_client)
    monkeypatch.setattr(heartbeat.logger, "info", record_info)
    monkeypatch.setattr(heartbeat.logger, "exception", record_exception)

    # When: the one-shot has a zero-round budget.
    reply = await heartbeat.run_agent_oneshot(agent_id, "task", max_rounds=0)

    # Then: it completes normally and records zero executed rounds.
    assert reply == ""
    assert client.closed is True
    assert exception_messages == []
    assert any("completed (0 rounds" in message for message in info_messages)


@pytest.mark.parametrize("path", ["heartbeat", "oneshot"])
@pytest.mark.parametrize("budget", [0, 1, 2], ids=["already-capped", "midloop-cap", "finish"])
async def test_daily_quota_charges_each_heartbeat_request_and_skips_when_capped(monkeypatch, path, budget):
    # Given: both heartbeat entry points share a bounded agent daily budget.
    from app.dao.agent_dao import agent_dao
    from app.dao.notification_dao import notification_dao

    tenant_id = uuid.uuid4()
    model = replace(_model(), tenant_id=tenant_id)
    agent = AgentRecord(
        id=uuid.uuid4(), name="Heartbeat", creator_id=uuid.uuid4(), tenant_id=tenant_id, primary_model_id=model.id
    )
    finish = _finish_response("done")
    finish.usage = {"total_tokens": 17}
    client = BackgroundClient([LLMResponse(content="continue", usage={"total_tokens": 17}), finish])
    charge = AsyncMock(side_effect=[agent] * budget + [None])
    usage, activity, execute, notify = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.dao.llm_dao.llm_model_dao.get", AsyncMock(return_value=model))
    monkeypatch.setattr("app.dao.activity_log_dao.agent_activity_log_dao.list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(notification_dao, "list_unread_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(heartbeat, "get_storage_backend", lambda: SimpleNamespace(exists=AsyncMock(return_value=False)))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("", "")))
    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.agent_tools.execute_tool", execute)
    monkeypatch.setattr("app.services.llm.create_fresh_llm_client_from_model", AsyncMock(return_value=(model, client)))
    monkeypatch.setattr("app.services.token_tracker.record_token_usage", usage)
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    monkeypatch.setattr(heartbeat, "_notify_oneshot_error", notify)
    complete = client.complete

    async def charged_complete(**kwargs):
        assert charge.await_count == len(client.messages_seen) + 1
        return await complete(**kwargs)

    monkeypatch.setattr(client, "complete", charged_complete)

    # When: the chosen entry point runs until finish or quota denial.
    if path == "heartbeat":
        await heartbeat._execute_heartbeat(agent.id)
    else:
        reply = await heartbeat.run_agent_oneshot(agent.id, "task")
        assert reply == ("done" if budget == 2 else "")

    # Then: refused requests never dispatch or post and prior token usage survives.
    assert len(client.messages_seen) == budget
    assert charge.await_count == (2 if budget == 2 else budget + 1)
    assert client.closed
    assert activity.await_count == int(budget == 2)
    assert usage.await_count == int(budget > 0)
    if budget:
        assert usage.await_args is not None
        assert usage.await_args.args[1].total_tokens == 17 * budget
    execute.assert_not_awaited()
    notify.assert_not_awaited()


class _TickHarness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.dao.agent_dao import agent_dao
        from app.dao.tenant_dao import tenant_dao

        self.agent_id: uuid.UUID = uuid.uuid4()
        self.agent: SimpleNamespace = SimpleNamespace(
            id=self.agent_id,
            name="Beat",
            is_expired=False,
            expires_at=None,
            tenant_id=None,
            heartbeat_active_hours=None,
            heartbeat_interval_minutes=1,
            last_heartbeat_at=None,
        )
        self.claims: int = 0
        self.in_claim: asyncio.Event = asyncio.Event()
        self.finish_claim: asyncio.Event = asyncio.Event()
        self.in_audit: asyncio.Event = asyncio.Event()
        self.finish_audit: asyncio.Event = asyncio.Event()
        self.block_claim: bool = False
        self.block_audit: bool = False
        self.executed: list[uuid.UUID] = []
        self.run_until: asyncio.Event = asyncio.Event()
        self.cancelled: list[uuid.UUID] = []
        self.claim_cancelled: bool = False
        self.fail: str | None = None
        self.block_candidates: bool = False
        self.in_candidates: asyncio.Event = asyncio.Event()
        self.finish_candidates: asyncio.Event = asyncio.Event()
        self.reconcile_mode: str = "return"
        self.reconciles: list[asyncio.Task[object] | None] = []
        self.reconcile_cancelled: asyncio.Event = asyncio.Event()
        self.finish_reconcile_cleanup: asyncio.Event = asyncio.Event()

        async def candidates() -> list[SimpleNamespace]:
            if self.fail == "candidates":
                raise OSError("candidate read failed")
            if self.block_candidates:
                self.in_candidates.set()
                await self.finish_candidates.wait()
            return [self.agent]

        async def claim(_agent_id: uuid.UUID, **_kwargs: object) -> bool:
            self.claims += 1
            if self.fail == "claim":
                raise OSError("claim failed")
            if self.block_claim:
                self.in_claim.set()
                try:
                    await self.finish_claim.wait()
                except asyncio.CancelledError:
                    self.claim_cancelled = True
                    raise
            return True

        async def audit(action: str, *_args: object, **_kwargs: object) -> None:
            if action == "heartbeat_fire" and self.block_audit:
                self.in_audit.set()
                await self.finish_audit.wait()

        async def execute(agent_id: uuid.UUID) -> None:
            try:
                await self.run_until.wait()
                self.executed.append(agent_id)
            except asyncio.CancelledError:
                self.cancelled.append(agent_id)
                raise

        async def reconcile(_now: object) -> None:
            self.reconciles.append(asyncio.current_task())
            match self.reconcile_mode:
                case "hang":
                    _ = await asyncio.Event().wait()
                case "slow_cancel":
                    try:
                        _ = await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.reconcile_cancelled.set()
                        _ = await self.finish_reconcile_cleanup.wait()
                        raise
                case "raise":
                    raise RuntimeError("docker unavailable")
                case _:
                    return

        async def no_tenants(_ids: object) -> list[object]:
            return []

        monkeypatch.setattr(agent_dao, "list_heartbeat_candidates", candidates)
        monkeypatch.setattr(agent_dao, "claim_heartbeat", claim)
        monkeypatch.setattr(tenant_dao, "get_by_ids", no_tenants)
        monkeypatch.setattr("app.services.agent_stops.reconcile_stopped_containers", reconcile)
        monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
        monkeypatch.setattr("app.services.timezone_utils.get_agent_timezone_sync", lambda *_args: "UTC")
        monkeypatch.setattr(heartbeat, "_is_in_active_hours", lambda *_args: True)
        monkeypatch.setattr(heartbeat, "_execute_heartbeat", execute)


@pytest.fixture
async def tick(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_TickHarness]:
    heartbeat.open_heartbeat_admission()
    try:
        yield _TickHarness(monkeypatch)
    finally:
        heartbeat.open_heartbeat_admission()


async def test_tick_claims_nothing_once_admission_closed(tick: _TickHarness) -> None:
    # Given a closed gate
    heartbeat.close_heartbeat_admission()
    # When
    await heartbeat._heartbeat_tick()
    # Then no heartbeat interval was consumed and no task exists
    assert tick.claims == 0
    assert not heartbeat._HEARTBEAT_TASKS


@pytest.mark.parametrize("stage", ["claim", "audit"])
async def test_claimed_heartbeat_still_starts_when_gate_closes_mid_tick(tick: _TickHarness, stage: str) -> None:
    # Given a tick suspended after it advanced last_heartbeat_at
    if stage == "claim":
        tick.block_claim = True
        suspended, resume = tick.in_claim, tick.finish_claim
    else:
        tick.block_audit = True
        suspended, resume = tick.in_audit, tick.finish_audit
    running = asyncio.create_task(heartbeat._heartbeat_tick())
    await suspended.wait()
    # When shutdown closes admission and drains before the awaits return
    heartbeat.close_heartbeat_admission()
    drain = asyncio.create_task(heartbeat.drain_heartbeat_tasks(grace_seconds=1))
    await asyncio.sleep(0)
    resume.set()
    await running
    await asyncio.sleep(0)
    tick.run_until.set()
    await drain
    # Then the claimed heartbeat ran to completion inside the grace period
    assert tick.executed == [tick.agent_id]
    assert tick.cancelled == []
    assert not heartbeat._HEARTBEAT_TASKS


async def test_claim_outliving_grace_is_cancelled_before_drain_returns(tick: _TickHarness) -> None:
    # Given a tick stuck in claim_heartbeat
    tick.block_claim = True
    running = asyncio.create_task(heartbeat._heartbeat_tick())
    await tick.in_claim.wait()
    # When the drain runs out of grace
    await heartbeat.drain_heartbeat_tasks(grace_seconds=0.01)
    # Then the claim was cancelled before the drain returned and no heartbeat starts afterwards
    assert tick.claim_cancelled
    await running
    tick.finish_claim.set()
    tick.run_until.set()
    await asyncio.sleep(0)
    assert tick.executed == []
    assert not heartbeat._HEARTBEAT_TASKS


async def test_heartbeat_cancelled_after_grace_without_touching_foreign_tasks(tick: _TickHarness) -> None:
    # Given a running heartbeat and a task owned elsewhere
    foreign = asyncio.create_task(asyncio.Event().wait())
    await heartbeat._heartbeat_tick()
    await asyncio.sleep(0)
    heartbeat.close_heartbeat_admission()
    # When the grace period expires
    await heartbeat.drain_heartbeat_tasks(grace_seconds=0.01)
    # Then only the heartbeat was cancelled
    assert tick.cancelled == [tick.agent_id]
    assert not heartbeat._HEARTBEAT_TASKS
    assert not foreign.done()
    foreign.cancel()
    _ = await asyncio.gather(foreign, return_exceptions=True)


async def test_reopened_gate_claims_again(tick: _TickHarness) -> None:
    # Given a gate closed by an earlier lifespan
    heartbeat.close_heartbeat_admission()
    heartbeat.open_heartbeat_admission()
    # When
    await heartbeat._heartbeat_tick()
    # Then
    assert tick.claims == 1
    tick.run_until.set()
    await heartbeat.drain_heartbeat_tasks(grace_seconds=1)


async def test_loop_survives_failed_tick_and_waits_between_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = 0
    waits: list[float] = []
    second_tick = asyncio.Event()
    cadence_entered = asyncio.Event()
    release = asyncio.Event()

    async def tick_once() -> None:
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise RuntimeError("tick failure")
        second_tick.set()

    async def cadence(seconds: float) -> None:
        waits.append(seconds)
        cadence_entered.set()
        await release.wait()
        release.clear()

    monkeypatch.setattr(heartbeat, "_heartbeat_tick", tick_once)
    monkeypatch.setattr("app.services.agent_stops.reconcile_stopped_containers", AsyncMock())
    monkeypatch.setattr(heartbeat.asyncio, "sleep", cadence)
    loop = asyncio.create_task(heartbeat.start_heartbeat())
    try:
        await asyncio.wait_for(cadence_entered.wait(), 1)
        assert ticks == 1
        release.set()
        await asyncio.wait_for(second_tick.wait(), 1)
        assert ticks == 2
        assert waits[0] == heartbeat.HEARTBEAT_INTERVAL_SECONDS
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


async def test_cancelling_loop_preserves_admitted_claim_for_drain(tick: _TickHarness) -> None:
    tick.block_claim = True
    loop = asyncio.create_task(heartbeat.start_heartbeat())
    await asyncio.wait_for(tick.in_claim.wait(), 1)
    heartbeat.close_heartbeat_admission()
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)
    drain = asyncio.create_task(heartbeat.drain_heartbeat_tasks(grace_seconds=1))
    tick.finish_claim.set()
    tick.run_until.set()
    await asyncio.wait_for(drain, 1)
    assert not tick.claim_cancelled
    assert tick.executed == [tick.agent_id]


async def test_heartbeat_loop_recovers_from_tick_failure_once_per_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks: list[int] = []
    waiting = asyncio.Event()
    advance = asyncio.Event()
    second_tick = asyncio.Event()

    async def tick() -> None:
        ticks.append(len(ticks) + 1)
        if len(ticks) == 1:
            raise RuntimeError("tick failed")
        second_tick.set()

    async def cadence(_seconds: float) -> None:
        waiting.set()
        await advance.wait()
        advance.clear()

    monkeypatch.setattr(heartbeat, "_heartbeat_tick", tick)
    monkeypatch.setattr("app.services.agent_stops.reconcile_stopped_containers", AsyncMock())
    monkeypatch.setattr(heartbeat.asyncio, "sleep", cadence)
    loop = asyncio.create_task(heartbeat.start_heartbeat())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        assert ticks == [1]
        assert not loop.done()
        advance.set()
        await asyncio.wait_for(second_tick.wait(), 1)
        assert ticks == [1, 2]
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


def _start_loop(worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch) -> asyncio.Task[object]:
    monkeypatch.setattr(heartbeat.asyncio, "sleep", worker_http.pacing.wait)
    return worker_http.start_loop("heartbeat", heartbeat.start_heartbeat(), budget_seconds=BUDGET)


@pytest.mark.parametrize("stage", ["candidates", "claim"])
async def test_swallowed_selection_or_claim_failure_never_beats(
    tick: _TickHarness, worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    # Given a loop whose ticks claim and finish their heartbeats
    tick.run_until.set()
    loop = _start_loop(worker_http, monkeypatch)
    await worker_http.pacing.paused()
    # When the tick swallows a failing candidate read or claim for longer than the budget
    tick.fail = stage
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health stays up and the loop keeps running
    await worker_http.ready(expected=False)
    assert not loop.done()
    # And the next tick whose selection and claims complete beats again
    tick.fail = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)
    await heartbeat.drain_heartbeat_tasks(grace_seconds=1)


async def test_hung_candidate_read_goes_stale_without_cancelling_the_loop(
    tick: _TickHarness, worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a loop whose candidate read hangs
    tick.run_until.set()
    tick.block_candidates = True
    loop = _start_loop(worker_http, monkeypatch)
    await asyncio.wait_for(tick.in_candidates.wait(), 2)
    # When the budget passes
    worker_http.pacing.now += BUDGET + 1
    # Then readiness fails and the loop is left running
    await worker_http.ready(expected=False)
    assert not loop.done()
    # And the pass that finally completes beats
    tick.finish_candidates.set()
    await worker_http.pacing.paused()
    await worker_http.ready(expected=True)
    await heartbeat.drain_heartbeat_tasks(grace_seconds=1)


@pytest.mark.parametrize("mode", ["hang", "raise"])
async def test_stopped_container_reconciliation_neither_blocks_nor_counts_as_progress(
    tick: _TickHarness, worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Given a stopped-container reconciliation that hangs forever or raises
    tick.run_until.set()
    tick.reconcile_mode = mode
    loop = _start_loop(worker_http, monkeypatch)
    # When the first tick runs after the budget has already passed
    worker_http.pacing.now += BUDGET + 1
    await worker_http.pacing.paused()
    # Then that tick beat
    await worker_http.ready(expected=True)
    # And so did the next one, with no second reconciliation beside a parked one
    await worker_http.pacing.step(BUDGET + 1)
    await worker_http.ready(expected=True)
    assert len(tick.reconciles) == (1 if mode == "hang" else 2)
    # When selection then fails past the budget while reconciliation is parked or failing
    tick.fail = "candidates"
    await worker_http.pacing.step(BUDGET + 1)
    # Then reconciliation never stood in for progress
    await worker_http.ready(expected=False)
    assert not loop.done()
    await heartbeat.drain_heartbeat_tasks(grace_seconds=1)


async def test_shutdown_cancels_and_awaits_the_parked_reconciliation(
    tick: _TickHarness, worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a loop whose stopped-container reconciliation is parked and cleans up slowly once cancelled
    tick.run_until.set()
    tick.reconcile_mode = "slow_cancel"
    loop = _start_loop(worker_http, monkeypatch)
    await worker_http.pacing.paused()
    await heartbeat.drain_heartbeat_tasks(grace_seconds=1)
    # When the runtime stops its owned loop
    stopping = asyncio.create_task(worker_http.runtime.tasks.stop())
    await asyncio.wait_for(tick.reconcile_cancelled.wait(), 2)
    # Then the loop cancelled its reconciliation and is still awaiting it
    assert not loop.done()
    tick.finish_reconcile_cleanup.set()
    await asyncio.wait_for(stopping, 2)
    (reconciliation,) = tick.reconciles
    assert reconciliation is not None
    assert reconciliation.cancelled()


async def test_heartbeat_executions_never_beat(
    tick: _TickHarness, worker_http: WorkerHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a heartbeat execution still running from the last successful tick
    loop = _start_loop(worker_http, monkeypatch)
    await worker_http.pacing.paused()
    # When later ticks fail past the budget and the execution then finishes
    tick.fail = "candidates"
    await worker_http.pacing.step(BUDGET + 1)
    tick.run_until.set()
    await heartbeat.drain_heartbeat_tasks(grace_seconds=1)
    # Then the finished execution did not count as loop progress
    assert tick.executed == [tick.agent_id]
    await worker_http.ready(expected=False)
    assert not loop.done()
