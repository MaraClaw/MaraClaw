import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.domain.agents import AgentStatus
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from app.runtime import lifecycle
from app.services import agent_start_reconciler as module
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


@pytest.mark.parametrize("dependency", ["recover", "start_container"])
async def test_start_caught_operation_error_does_not_beat_for_a_selected_row(
    monkeypatch: pytest.MonkeyPatch,
    worker_http: WorkerHTTP,
    start_operation: AgentStartOperation,
    dependency: str,
) -> None:
    recovery = AsyncMock(side_effect=OSError("private recovery failure"))
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

    async def first_cleanup() -> None:
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
