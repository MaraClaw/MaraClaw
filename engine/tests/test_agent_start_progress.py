import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pywhales import ClientNotFoundError
from pywhales.client_config import ParsingError
from pywhales.exceptions import DockerException

from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from app.runtime import lifecycle
from app.services import agent_start_reconciler as module
from app.services.guest_admission import GuestAdmissionError, GuestStartStoppedError
from producer_admission_fixtures import producer_admission
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["producer_admission", "worker_http"]


@pytest.fixture
def start_operation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AgentStartOperation:
    operation = AgentStartOperation(uuid4(), uuid4(), "pending")
    agent = AgentRecord(id=operation.agent_id, name="Guest", creator_id=uuid4(), status=AgentStatus.IDLE)
    monkeypatch.setattr(module.start_store, "outstanding", AsyncMock(return_value=[operation]))
    monkeypatch.setattr(module.start_store, "recover", AsyncMock(return_value=agent))
    monkeypatch.setattr(module.start_store, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(module.start_store, "publish", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "assert_no_active_run", AsyncMock())
    monkeypatch.setattr(module, "flush_request_transaction", AsyncMock())
    monkeypatch.setattr(module.agent_manager, "_agent_dir", lambda identifier: tmp_path / str(identifier))
    monkeypatch.setattr(module.agent_manager, "start_container", AsyncMock(return_value=None))
    return operation


async def test_start_idle_and_caught_scan_errors_observe_last_completed_pass(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    pacing, runtime = worker_http.pacing, worker_http.runtime
    outstanding = AsyncMock(return_value=[])
    monkeypatch.setattr(module.start_store, "outstanding", outstanding)
    monkeypatch.setattr(module, "sleep", pacing.wait)
    runtime.tasks.start("start", module.agent_start_reconciler_loop(), requirement="loop", progress_budget_seconds=300)
    await pacing.paused()
    for _ in range(4):
        await pacing.step()
        await worker_http.ready(expected=True)
    outstanding.side_effect = OSError("private database failure")
    for _ in range(4):
        await pacing.step()
    await worker_http.ready(expected=False)
    assert not runtime.tasks.required["start"][0].done()
    outstanding.side_effect = None
    await pacing.step()
    await worker_http.ready(expected=True)


@pytest.mark.parametrize(
    ("dependency", "fault"),
    [
        ("recover", OSError("private recovery failure")),
        ("start_container", OSError("private runtime failure")),
        ("start_container", ParsingError("truncated inspect")),
        ("start_container", ClientNotFoundError("missing client")),
        ("start_container", ValueError("setup incomplete")),
    ],
)
async def test_start_caught_operation_error_does_not_beat_for_a_selected_row(
    monkeypatch: pytest.MonkeyPatch,
    worker_http: WorkerHTTP,
    start_operation: AgentStartOperation,
    dependency: str,
    fault: Exception,
) -> None:
    recovery = AsyncMock(side_effect=fault)
    if dependency == "recover":
        monkeypatch.setattr(module.start_store, "recover", recovery)
    else:
        monkeypatch.setattr(module.agent_manager, "start_container", recovery)
        monkeypatch.setattr(module.start_store, "fail", AsyncMock())
    monkeypatch.setattr(module, "sleep", worker_http.pacing.wait)
    worker_http.runtime.tasks.start(
        "start", module.agent_start_reconciler_loop(), requirement="loop", progress_budget_seconds=300
    )
    await worker_http.pacing.paused()
    for _ in range(4):
        await worker_http.pacing.step()
    await worker_http.ready(expected=False)
    assert start_operation.state == "pending"
    assert not worker_http.runtime.tasks.required["start"][0].done()
    recovery.side_effect, recovery.return_value = None, None
    if dependency == "recover":
        recovery.return_value = AgentRecord(
            id=start_operation.agent_id, name="Guest", creator_id=uuid4(), status=AgentStatus.IDLE
        )
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)


async def test_start_poisoned_operation_does_not_block_healthy_rows_or_loop(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, start_operation: AgentStartOperation
) -> None:
    healthy = AgentStartOperation(uuid4(), uuid4(), "pending")
    fault = ParsingError("truncated inspect")

    async def recover(operation: AgentStartOperation) -> AgentRecord:
        return AgentRecord(id=operation.agent_id, name="Guest", creator_id=uuid4(), status=AgentStatus.IDLE)

    async def start(agent: AgentRecord) -> None:
        if agent.id == start_operation.agent_id:
            raise fault

    published, reported = AsyncMock(return_value=True), Mock()
    monkeypatch.setattr(module.start_store, "outstanding", AsyncMock(return_value=[start_operation, healthy]))
    monkeypatch.setattr(module.start_store, "recover", recover)
    monkeypatch.setattr(module.start_store, "publish", published)
    monkeypatch.setattr(module.start_store, "fail", AsyncMock())
    monkeypatch.setattr(module.agent_manager, "start_container", start)
    monkeypatch.setattr(module, "report_exception", reported, raising=False)
    monkeypatch.setattr(module, "sleep", worker_http.pacing.wait)

    task = worker_http.start_loop("start", module.agent_start_reconciler_loop(), budget_seconds=300)
    await worker_http.pacing.paused()
    for _ in range(4):
        await worker_http.pacing.step()
        await worker_http.ready(expected=True)

    assert not task.done()
    assert [call.args[0] for call in published.await_args_list] == [healthy] * 5
    assert [call.args[0] for call in reported.call_args_list] == [fault] * 5


async def test_start_retry_backoff_does_not_hide_an_unhealthy_loop(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, start_operation: AgentStartOperation
) -> None:
    pacing = worker_http.pacing
    refused = AgentStartOperation(uuid4(), uuid4(), "pending")
    outstanding = AsyncMock(side_effect=[[start_operation], [], [start_operation], [refused], [], []])
    start = AsyncMock(
        side_effect=[
            ParsingError("truncated inspect"),
            ParsingError("truncated inspect"),
            GuestAdmissionError("guest_memory_capacity_exceeded", "capacity exhausted"),
        ]
    )
    halted = AsyncMock(return_value=None)
    monkeypatch.setattr(module.start_store, "outstanding", outstanding)
    monkeypatch.setattr(module.start_store, "halted", halted)
    monkeypatch.setattr(module.start_store, "fail", AsyncMock())
    monkeypatch.setattr(module.agent_manager, "start_container", start)
    monkeypatch.setattr(module, "sleep", pacing.wait)

    worker_http.start_loop("start", module.agent_start_reconciler_loop(), budget_seconds=300)
    await pacing.paused()
    for _ in range(4):
        await pacing.step()
    await worker_http.ready(expected=False)
    assert start.await_count == 3

    halted.return_value = "cancelled"
    await pacing.step()
    await worker_http.ready(expected=True)


async def test_start_unexpected_scan_failure_backs_off_and_recovers(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    outstanding = AsyncMock(side_effect=ValueError("invalid scan result"))
    pacing = worker_http.pacing
    monkeypatch.setattr(module.start_store, "outstanding", outstanding)
    monkeypatch.setattr(module, "sleep", pacing.wait)
    task = worker_http.start_loop("start", module.agent_start_reconciler_loop(), budget_seconds=300)

    for delay in (1, 2, 4, 8, 16, 30, 30):
        assert await asyncio.wait_for(pacing.entered.get(), 2) == delay
        pacing.now += 100
        pacing.go.put_nowait(None)
    assert await asyncio.wait_for(pacing.entered.get(), 2) == 30
    await worker_http.ready(expected=False)
    assert not task.done()

    outstanding.side_effect, outstanding.return_value = None, []
    pacing.go.put_nowait(None)
    assert await asyncio.wait_for(pacing.entered.get(), 2) == 10
    await worker_http.ready(expected=True)
    outstanding.side_effect = ValueError("another scan failure")
    pacing.go.put_nowait(None)
    assert await asyncio.wait_for(pacing.entered.get(), 2) == 1


@pytest.mark.parametrize("reason", ["cancelled", "ineligible"])
async def test_start_stopped_at_a_checkpoint_is_superseded_without_failure_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    worker_http: WorkerHTTP,
    start_operation: AgentStartOperation,
    reason: str,
) -> None:
    # Given a reconciler start that a Stop, or the agent no longer qualifying, halts at a checkpoint.
    halted = AsyncMock(side_effect=GuestStartStoppedError("cancelled" if reason == "cancelled" else "ineligible"))
    fail, cleanup = AsyncMock(), AsyncMock()
    log = SimpleNamespace(info=Mock(), warning=Mock(), error=Mock())
    monkeypatch.setattr(module.agent_manager, "start_container", halted)
    monkeypatch.setattr(module.start_store, "fail", fail)
    monkeypatch.setattr(module, "cleanup_cancelled_start", cleanup)
    monkeypatch.setattr(module, "logger", log)
    monkeypatch.setattr(module, "sleep", worker_http.pacing.wait)
    # When the reconcile loop runs it.
    worker_http.runtime.tasks.start(
        "start", module.agent_start_reconciler_loop(), requirement="loop", progress_budget_seconds=300
    )
    await worker_http.pacing.paused()
    for _ in range(4):
        await worker_http.pacing.step()
    # Then each pass completes as superseded: settled like an unpublished start, with no failure code,
    # no error log and progress still observed.
    await worker_http.ready(expected=True)
    fail.assert_not_awaited()
    cleanup.assert_awaited_with(start_operation)
    log.warning.assert_not_called()
    log.error.assert_not_called()
    log.info.assert_called()


async def test_stopped_start_releases_the_request_transaction_before_recovery_locks_the_agent(
    monkeypatch: pytest.MonkeyPatch, start_operation: AgentStartOperation
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        module.agent_manager, "start_container", AsyncMock(side_effect=GuestStartStoppedError("cancelled"))
    )
    monkeypatch.setattr(module, "flush_request_transaction", AsyncMock(side_effect=lambda: calls.append("flush")))
    monkeypatch.setattr(
        module.start_store, "recover", AsyncMock(side_effect=lambda _operation: calls.append("recover"))
    )
    monkeypatch.setattr(module, "cleanup_cancelled_start", AsyncMock(side_effect=lambda _op: calls.append("cleanup")))
    agent = AgentRecord(id=start_operation.agent_id, name="Guest", creator_id=uuid4(), status=AgentStatus.IDLE)

    with pytest.raises(GuestStartStoppedError):
        _ = await module._execute_start(start_operation, agent)

    assert calls == ["flush", "recover", "cleanup"]


@pytest.mark.parametrize(
    "failure",
    [
        DockerException(["docker", "stop"], 1, stderr=b"daemon unavailable"),
        OSError("pool down"),
        ParsingError("truncated inspect"),
        ClientNotFoundError("missing client"),
    ],
)
async def test_failed_settlement_never_hides_the_stop(
    monkeypatch: pytest.MonkeyPatch, start_operation: AgentStartOperation, failure: Exception
) -> None:
    monkeypatch.setattr(
        module.agent_manager, "start_container", AsyncMock(side_effect=GuestStartStoppedError("ineligible"))
    )
    fail = AsyncMock()
    monkeypatch.setattr(module.start_store, "fail", fail)
    monkeypatch.setattr(module, "cleanup_cancelled_start", AsyncMock(side_effect=failure))
    agent = AgentRecord(id=start_operation.agent_id, name="Guest", creator_id=uuid4(), status=AgentStatus.IDLE)

    with pytest.raises(GuestStartStoppedError) as stop:
        _ = await module.execute_start(start_operation, agent)

    assert stop.value.reason == "ineligible"
    fail.assert_not_awaited()


@pytest.mark.parametrize("dependency", ["outstanding", "recover", "reserve", "publish"])
async def test_start_dependency_stall_is_observed_without_replacing_pending_intent(
    monkeypatch: pytest.MonkeyPatch,
    worker_http: WorkerHTTP,
    start_operation: AgentStartOperation,
    dependency: str,
) -> None:
    pacing, runtime = worker_http.pacing, worker_http.runtime
    entered, release = asyncio.Event(), asyncio.Event()
    result: object = True
    if dependency == "outstanding":
        result = [start_operation]
    elif dependency == "recover":
        result = AgentRecord(id=start_operation.agent_id, name="Guest", creator_id=uuid4(), status=AgentStatus.IDLE)

    async def blocked(*_args: object, **_kwargs: object) -> object:
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(module.start_store, dependency, blocked)
    monkeypatch.setattr(module, "sleep", pacing.wait)
    runtime.tasks.start("start", module.agent_start_reconciler_loop(), requirement="loop", progress_budget_seconds=300)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pacing.now += 301
        await worker_http.ready(expected=False)
        assert start_operation.state == "pending"
        assert not runtime.tasks.required["start"][0].done()
    finally:
        release.set()
    await pacing.paused()
    await worker_http.ready(expected=True)
    assert start_operation.state == "pending"


async def test_start_guest_contract_is_exempt_and_shutdown_drains_publication(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP, start_operation: AgentStartOperation
) -> None:
    pacing, runtime = worker_http.pacing, worker_http.runtime
    entered, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    published: list[AgentStartOperation] = []

    async def start(_agent: AgentRecord) -> None:
        entered.set()
        await release.wait()

    async def publish(operation: AgentStartOperation, _agent: AgentRecord) -> bool:
        published.append(operation)
        return True

    async def first_cleanup(_grace_seconds: float) -> None:
        stopping.set()

    monkeypatch.setattr(module.agent_manager, "start_container", start)
    monkeypatch.setattr(module.start_store, "publish", publish)
    monkeypatch.setattr(module, "sleep", pacing.wait)
    monkeypatch.setattr(lifecycle, "drain_background_tasks", first_cleanup)
    runtime.tasks.start("start", module.agent_start_reconciler_loop(), requirement="loop", progress_budget_seconds=300)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        pacing.now += 10000
        await worker_http.ready(expected=True)
        stop = asyncio.create_task(runtime.stop())
        try:
            await asyncio.wait_for(stopping.wait(), 2)
            await worker_http.ready(expected=False)
            assert not stop.done()
            assert published == []
        finally:
            release.set()
            await asyncio.wait_for(stop, 2)
    finally:
        release.set()
    assert published == [start_operation]
    assert runtime.tasks.required["start"][0].cancelled()
