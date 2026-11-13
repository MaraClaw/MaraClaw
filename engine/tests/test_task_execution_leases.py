import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest

from app.core.domain.tasks import TaskStatus, TaskType
from app.db.errors import DbError
from app.records.task import TaskClaim, TaskRecord
from app.services import activity_logger, agent_context, llm, task_executor, task_lease
from app.services.llm.background_guard import BackgroundCallOutcome
from worker_progress_fixtures import WorkerHTTP, worker_http

__all__ = ["worker_http"]


@pytest.fixture
def activity(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    logged = AsyncMock()
    monkeypatch.setattr(activity_logger, "log_activity", logged)
    return logged


@pytest.fixture
def execution(monkeypatch: pytest.MonkeyPatch, activity: AsyncMock):
    task = TaskRecord(id=uuid4(), agent_id=uuid4(), created_by=uuid4(), title="Ship")
    claim = TaskClaim(task.id, uuid4())
    monkeypatch.setattr(task_executor, "claim_task", AsyncMock(return_value=claim))
    monkeypatch.setattr(task_executor.task_dao, "renew", AsyncMock(return_value=True))
    monkeypatch.setattr(task_executor.task_dao, "get_for_agent", AsyncMock(return_value=task))
    monkeypatch.setattr(
        task_executor.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(name="Agent", role_description=""))
    )
    monkeypatch.setattr(agent_context, "build_agent_context", AsyncMock(return_value=("static", "dynamic")))
    monkeypatch.setattr(
        llm, "call_agent_llm_with_tools", AsyncMock(return_value=BackgroundCallOutcome("success", "Shipped"))
    )
    released = AsyncMock(return_value=True)
    completed = AsyncMock(return_value=True)
    monkeypatch.setattr(task_executor.task_dao, "release", released)
    monkeypatch.setattr(task_executor.task_dao, "complete", completed)
    return task, claim, released, completed


@pytest.mark.parametrize("task_type", list(TaskType))
@pytest.mark.parametrize("failure", ["agent", "context", "llm"])
async def test_failed_execution_releases_both_task_types_with_reason(monkeypatch, execution, task_type, failure):
    task, claim, released, completed = execution
    task.type = task_type
    match failure:
        case "agent":
            monkeypatch.setattr(task_executor.agent_dao, "get", AsyncMock(return_value=None))
            reason = "Digital employee not found"
        case "context":
            monkeypatch.setattr(
                agent_context, "build_agent_context", AsyncMock(side_effect=RuntimeError("context bad"))
            )
            reason = "Failed to build agent context: context bad"
        case "llm":
            monkeypatch.setattr(llm, "call_agent_llm_with_tools", AsyncMock(side_effect=RuntimeError("model bad")))
            reason = "Execution error: model bad"
        case _:
            pytest.fail("unknown test scenario")

    await task_executor.execute_task(task.id, task.agent_id)

    released.assert_awaited_once_with(claim, log=f"❌ {reason}")
    completed.assert_not_awaited()


@pytest.mark.parametrize("task_type", list(TaskType))
async def test_success_settles_todo_done_and_supervision_pending(execution, task_type):
    task, claim, released, completed = execution
    task.type = task_type

    await task_executor.execute_task(task.id, task.agent_id)

    completed.assert_awaited_once()
    assert completed.await_args.args == (claim,)
    assert completed.await_args.kwargs["status"] == (
        TaskStatus.DONE if task_type == TaskType.TODO else TaskStatus.PENDING
    )
    assert "Shipped" in completed.await_args.kwargs["log"]
    released.assert_not_awaited()


@pytest.mark.parametrize("task_type", list(TaskType))
async def test_cancellation_releases_claim_and_propagates(monkeypatch, execution, task_type):
    task, claim, released, completed = execution
    task.type = task_type
    started = asyncio.Event()

    async def model(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(llm, "call_agent_llm_with_tools", model)
    run = asyncio.create_task(task_executor.execute_task(task.id, task.agent_id))
    await asyncio.wait_for(started.wait(), timeout=2)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    released.assert_awaited_once_with(claim, log="❌ Execution was cancelled before it finished")
    completed.assert_not_awaited()


async def test_losing_entry_claim_never_starts_agent_or_model(monkeypatch, execution):
    task, _claim, released, completed = execution
    monkeypatch.setattr(task_executor, "claim_task", AsyncMock(return_value=None))
    agent = AsyncMock()
    model = AsyncMock()
    monkeypatch.setattr(task_executor.agent_dao, "get", agent)
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", model)

    await task_executor.execute_task(task.id, task.agent_id)

    agent.assert_not_awaited()
    model.assert_not_awaited()
    released.assert_not_awaited()
    completed.assert_not_awaited()


async def test_preclaimed_callback_with_lost_token_never_starts_model(monkeypatch, execution):
    task, claim, _released, completed = execution
    monkeypatch.setattr(task_executor.task_dao, "renew", AsyncMock(return_value=False))
    model = AsyncMock()
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", model)

    await task_executor.execute_task(task.id, task.agent_id, claim)

    model.assert_not_awaited()
    completed.assert_not_awaited()


@pytest.mark.parametrize("renewal", [False, RuntimeError("database unavailable")])
async def test_heartbeat_failure_cancels_active_work(monkeypatch, renewal):
    started, cancelled = anyio.Event(), anyio.Event()
    claim = TaskClaim(uuid4(), uuid4())

    async def tick(_seconds):
        await started.wait()

    async def work(_guard):
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()

    monkeypatch.setattr(task_lease.anyio, "sleep", tick)
    renew = AsyncMock(side_effect=[True, renewal])
    monkeypatch.setattr(task_lease.task_dao, "renew", renew)

    with anyio.fail_after(2), pytest.raises(task_lease.TaskLeaseLostError):
        await task_lease.run_under_lease(claim, work)

    assert cancelled.is_set()


async def test_recovery_drains_batches_without_executing_tasks(monkeypatch):
    recovered = AsyncMock(side_effect=[[uuid4(), uuid4()], [uuid4()]])
    execute = AsyncMock(side_effect=AssertionError("recovery must not rerun"))
    monkeypatch.setattr(task_lease, "RECOVERY_BATCH", 2)
    monkeypatch.setattr(task_lease.task_dao, "recover_expired", recovered)
    monkeypatch.setattr(task_executor, "execute_task", execute)

    assert await task_lease.recover_expired_leases() == 3
    execute.assert_not_awaited()


@pytest.mark.parametrize("task_type", list(TaskType))
@pytest.mark.parametrize("content", ["[Error] provider down", "⚠️ Both models failed | Primary: a | Fallback: b"])
async def test_failure_outcome_releases_with_reason_instead_of_settling(
    monkeypatch, execution, activity, task_type, content
):
    task, claim, released, completed = execution
    task.type = task_type
    monkeypatch.setattr(
        llm, "call_agent_llm_with_tools", AsyncMock(return_value=BackgroundCallOutcome("failure", content))
    )

    await task_executor.execute_task(task.id, task.agent_id)

    released.assert_awaited_once_with(claim, log=f"❌ {content}")
    completed.assert_not_awaited()
    activity.assert_not_awaited()


async def test_model_runs_under_a_lease_guard_for_the_claim(monkeypatch, execution):
    task, claim, _released, _completed = execution
    model = AsyncMock(return_value=BackgroundCallOutcome("success", "Shipped"))
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", model)

    await task_executor.execute_task(task.id, task.agent_id)

    assert model.await_args is not None
    guard = model.await_args.kwargs["guard"]
    assert isinstance(guard, task_lease.TaskLeaseGuard)
    assert guard.claim == claim


async def test_revoked_claim_stops_before_the_next_round_and_writes_nothing(monkeypatch, execution, activity):
    task, _claim, released, completed = execution
    released.return_value = False
    monkeypatch.setattr(task_executor.task_dao, "renew", AsyncMock(side_effect=[True, True, False]))
    rounds: list[str] = []

    async def model(*, guard, **_kwargs):
        await guard.confirm()
        rounds.append("first")
        await guard.confirm()
        rounds.append("second")
        return BackgroundCallOutcome("success", "late")

    monkeypatch.setattr(llm, "call_agent_llm_with_tools", model)

    await task_executor.execute_task(task.id, task.agent_id)

    assert rounds == ["first"]
    completed.assert_not_awaited()
    activity.assert_not_awaited()


async def test_wall_clock_jump_without_monotonic_progress_expires_the_guard(monkeypatch):
    wall = [1_000.0]
    monkeypatch.setattr(task_lease, "time", SimpleNamespace(time=lambda: wall[0]))
    monkeypatch.setattr(task_lease.task_dao, "renew", AsyncMock(return_value=True))

    async def work(guard):
        guard.ensure_held()
        wall[0] += task_lease.TASK_LEASE_SECONDS
        with pytest.raises(task_lease.TaskLeaseLostError):
            guard.ensure_held()
        with pytest.raises(task_lease.TaskLeaseLostError):
            await guard.confirm()
        return "ran"

    assert await task_lease.run_under_lease(TaskClaim(uuid4(), uuid4()), work) == "ran"


async def test_wall_clock_jump_cancels_active_work(monkeypatch):
    wall = [1_000.0]
    started, cancelled = anyio.Event(), anyio.Event()
    monkeypatch.setattr(task_lease, "time", SimpleNamespace(time=lambda: wall[0]))
    monkeypatch.setattr(task_lease.task_dao, "renew", AsyncMock(return_value=True))

    async def tick(_seconds):
        await started.wait()
        wall[0] += task_lease.TASK_LEASE_SECONDS

    async def work(_guard):
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()

    monkeypatch.setattr(task_lease.anyio, "sleep", tick)

    with anyio.fail_after(2), pytest.raises(task_lease.TaskLeaseLostError):
        await task_lease.run_under_lease(TaskClaim(uuid4(), uuid4()), work)

    assert cancelled.is_set()


async def test_idle_recovery_passes_beat_so_readiness_outlives_the_budget(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given the recovery loop as a budgeted worker loop with nothing to recover.
    monkeypatch.setattr(task_lease.task_dao, "recover_expired", AsyncMock(return_value=[]))
    monkeypatch.setattr(task_lease.anyio, "sleep", worker_http.pacing.wait)
    task = worker_http.start_loop(
        "task_lease_recovery", task_lease.task_lease_recovery_loop(), budget_seconds=task_lease.PROGRESS_BUDGET_SECONDS
    )
    await worker_http.pacing.paused()
    # When four idle passes carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
        # Then each finished pass has beaten.
        await worker_http.ready(expected=True)
    assert not task.done()


async def test_failed_recovery_pass_does_not_beat_so_readiness_goes_stale_then_recovers(
    monkeypatch: pytest.MonkeyPatch, worker_http: WorkerHTTP
) -> None:
    # Given a healthy first pass, then a database that rejects the recovery write.
    recover = AsyncMock(return_value=[])
    monkeypatch.setattr(task_lease.task_dao, "recover_expired", recover)
    monkeypatch.setattr(task_lease.anyio, "sleep", worker_http.pacing.wait)
    task = worker_http.start_loop(
        "task_lease_recovery", task_lease.task_lease_recovery_loop(), budget_seconds=task_lease.PROGRESS_BUDGET_SECONDS
    )
    await worker_http.pacing.paused()
    recover.side_effect = DbError("recovery write failed")
    # When failing passes alone carry the clock past the budget.
    for _ in range(4):
        await worker_http.pacing.step()
    # Then readiness fails while health holds and the loop keeps running.
    await worker_http.ready(expected=False)
    assert not task.done()
    # And the next successful pass beats.
    recover.side_effect = None
    await worker_http.pacing.step()
    await worker_http.ready(expected=True)
