import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import pytest

from app.core.domain.tasks import TaskStatus, TaskType
from app.records.task import TaskClaim, TaskRecord
from app.services import agent_context, llm, task_executor, task_lease


@pytest.fixture
def execution(monkeypatch: pytest.MonkeyPatch):
    task = TaskRecord(id=uuid4(), agent_id=uuid4(), created_by=uuid4(), title="Ship")
    claim = TaskClaim(task.id, uuid4())
    monkeypatch.setattr(task_executor, "claim_task", AsyncMock(return_value=claim))
    monkeypatch.setattr(task_executor.task_dao, "renew", AsyncMock(return_value=True))
    monkeypatch.setattr(task_executor.task_dao, "get_for_agent", AsyncMock(return_value=task))
    monkeypatch.setattr(
        task_executor.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(name="Agent", role_description=""))
    )
    monkeypatch.setattr(agent_context, "build_agent_context", AsyncMock(return_value=("static", "dynamic")))
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", AsyncMock(return_value="Shipped"))
    released = AsyncMock(return_value=True)
    completed = AsyncMock(return_value=True)
    monkeypatch.setattr(task_executor.task_dao, "release", released)
    monkeypatch.setattr(task_executor.task_dao, "complete", completed)
    monkeypatch.setattr("app.services.activity_logger.log_activity", AsyncMock())
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

    async def work():
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
