import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.domain.tasks import TaskStatus, TaskType
from app.records.agent import AgentRecord
from app.records.task import TaskClaim, TaskRecord
from app.services import activity_logger, agent_context, llm, task_executor, task_lease
from app.services.llm.background_guard import BackgroundCallOutcome


@pytest.fixture
def logs() -> list[str]:
    return []


@pytest.fixture
def activity(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    logged = AsyncMock()
    monkeypatch.setattr(activity_logger, "log_activity", logged)
    return logged


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, logs: list[str], activity: AsyncMock) -> TaskRecord:
    task = TaskRecord(id=uuid4(), agent_id=uuid4(), title="Report", created_by=uuid4())

    async def claim(*_args: object, **_kwargs: object) -> TaskClaim | None:
        if task.claim_token is not None:
            return None
        task.claim_token = uuid4()
        task.status = TaskStatus.DOING
        return TaskClaim(task.id, task.claim_token)

    async def renew(owner: TaskClaim, **_kwargs: object) -> bool:
        return owner.token == task.claim_token

    async def settle(owner: TaskClaim, *, log: str, status: TaskStatus = TaskStatus.PENDING) -> bool:
        if owner.token != task.claim_token:
            return False
        task.claim_token = None
        task.status = status
        task.completed_at = datetime.now(UTC) if status == TaskStatus.DONE else None
        logs.append(log)
        return True

    monkeypatch.setattr(task_executor.task_dao, "claim", claim)
    monkeypatch.setattr(task_executor.task_dao, "renew", renew)
    monkeypatch.setattr(task_executor.task_dao, "release", settle)
    monkeypatch.setattr(task_executor.task_dao, "complete", settle)
    monkeypatch.setattr(task_executor.task_dao, "get_for_agent", AsyncMock(return_value=task))
    monkeypatch.setattr(
        task_executor.agent_dao, "get", AsyncMock(return_value=AgentRecord(task.agent_id, "Alex", task.created_by))
    )
    monkeypatch.setattr(agent_context, "build_agent_context", AsyncMock(return_value=("static", "dynamic")))
    monkeypatch.setattr(
        llm, "call_agent_llm_with_tools", AsyncMock(return_value=BackgroundCallOutcome("success", "Report delivered"))
    )
    return task


@pytest.mark.parametrize("task_type", [TaskType.TODO, TaskType.SUPERVISION])
@pytest.mark.parametrize("failure", ["agent", "context", "llm", "cancel"])
async def test_failed_run_releases_claim_and_logs_reason(
    run: TaskRecord, monkeypatch: pytest.MonkeyPatch, task_type: TaskType, failure: str, logs: list[str]
) -> None:
    run.type = task_type
    reasons = {
        "agent": "Digital employee not found",
        "context": "context broke",
        "llm": "model broke",
        "cancel": "cancelled",
    }
    if failure == "agent":
        monkeypatch.setattr(task_executor.agent_dao, "get", AsyncMock(return_value=None))
    elif failure == "context":
        monkeypatch.setattr(agent_context, "build_agent_context", AsyncMock(side_effect=RuntimeError("context broke")))
    elif failure == "llm":
        monkeypatch.setattr(llm, "call_agent_llm_with_tools", AsyncMock(side_effect=RuntimeError("model broke")))
    else:
        monkeypatch.setattr(llm, "call_agent_llm_with_tools", AsyncMock(side_effect=asyncio.CancelledError()))
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task_executor.execute_task(run.id, run.agent_id)
    else:
        await task_executor.execute_task(run.id, run.agent_id)
    assert run.status == TaskStatus.PENDING
    assert run.claim_token is None
    assert run.completed_at is None
    assert reasons[failure] in logs[-1]


@pytest.mark.parametrize("task_type", [TaskType.TODO, TaskType.SUPERVISION])
async def test_success_settles_with_expected_status(run: TaskRecord, task_type: TaskType, logs: list[str]) -> None:
    run.type = task_type
    await task_executor.execute_task(run.id, run.agent_id)
    assert run.claim_token is None
    assert run.status == (TaskStatus.DONE if task_type == TaskType.TODO else TaskStatus.PENDING)
    assert (run.completed_at is not None) == (task_type == TaskType.TODO)
    assert "Report delivered" in logs[-1]


async def test_duplicate_executor_entry_does_not_call_llm(run: TaskRecord, monkeypatch: pytest.MonkeyPatch) -> None:
    run.claim_token = uuid4()
    call = AsyncMock()
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", call)
    await task_executor.execute_task(run.id, run.agent_id)
    call.assert_not_awaited()


async def test_preclaimed_callback_with_stale_token_does_not_run(
    run: TaskRecord, monkeypatch: pytest.MonkeyPatch, logs: list[str]
) -> None:
    run.claim_token = uuid4()
    run.status = TaskStatus.DOING
    call = AsyncMock()
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", call)
    await task_executor.execute_task(run.id, run.agent_id, TaskClaim(run.id, uuid4()))
    call.assert_not_awaited()
    assert run.status == TaskStatus.DOING
    assert logs == []


@pytest.mark.parametrize("renew_failure", [False, RuntimeError("database unavailable")])
async def test_renewal_failure_cancels_active_llm_and_releases(
    run: TaskRecord, monkeypatch: pytest.MonkeyPatch, renew_failure: bool | RuntimeError, logs: list[str]
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def clock(_seconds: float) -> None:
        await started.wait()

    async def call(**_kwargs: object) -> BackgroundCallOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return BackgroundCallOutcome("success", "unreachable")

    monkeypatch.setattr(task_lease.anyio, "sleep", clock)
    monkeypatch.setattr(task_executor.task_dao, "renew", AsyncMock(side_effect=[True, renew_failure]))
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", call)
    await asyncio.wait_for(task_executor.execute_task(run.id, run.agent_id), timeout=2)
    assert cancelled.is_set()
    assert run.status == TaskStatus.PENDING
    assert run.claim_token is None
    assert "renewal failed" in logs[-1]


@pytest.mark.parametrize("task_type", [TaskType.TODO, TaskType.SUPERVISION])
async def test_failure_outcome_returns_to_pending_with_the_reason_logged(
    run: TaskRecord, monkeypatch: pytest.MonkeyPatch, task_type: TaskType, logs: list[str], activity: AsyncMock
) -> None:
    run.type = task_type
    outcome = BackgroundCallOutcome("failure", "[Error] provider down")
    monkeypatch.setattr(llm, "call_agent_llm_with_tools", AsyncMock(return_value=outcome))
    await task_executor.execute_task(run.id, run.agent_id)
    assert run.status == TaskStatus.PENDING
    assert run.claim_token is None
    assert run.completed_at is None
    assert logs == ["❌ [Error] provider down"]
    activity.assert_not_awaited()


async def test_revoked_run_stops_at_its_next_checkpoint_without_settle_log_or_activity(
    run: TaskRecord, monkeypatch: pytest.MonkeyPatch, logs: list[str], activity: AsyncMock
) -> None:
    rounds: list[str] = []

    async def call(*, guard: task_lease.TaskLeaseGuard, **_kwargs: object) -> BackgroundCallOutcome:
        await guard.confirm()
        rounds.append("first")
        run.status, run.claim_token = TaskStatus.DONE, None  # a PATCH or update_status revoked the run
        await guard.confirm()
        rounds.append("second")
        return BackgroundCallOutcome("success", "late")

    monkeypatch.setattr(llm, "call_agent_llm_with_tools", call)
    await task_executor.execute_task(run.id, run.agent_id)
    assert rounds == ["first"]
    assert run.status == TaskStatus.DONE
    assert logs == []
    activity.assert_not_awaited()


async def test_recovery_does_not_execute_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    recover = AsyncMock(side_effect=[[uuid4()] * task_lease.RECOVERY_BATCH, [uuid4()]])
    execute = AsyncMock()
    monkeypatch.setattr(task_lease.task_dao, "recover_expired", recover)
    monkeypatch.setattr(task_executor, "execute_task", execute)
    assert await task_lease.recover_expired_leases() == task_lease.RECOVERY_BATCH + 1
    execute.assert_not_awaited()


async def test_delayed_renewal_response_does_not_extend_the_confirmed_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]
    started, cancelled = asyncio.Event(), asyncio.Event()
    renewals = 0

    async def renew(*_args: object, **_kwargs: object) -> bool:
        nonlocal renewals
        renewals += 1
        clock[0] = 80.0 if renewals == 1 else 121.0
        return True

    async def advance(seconds: float) -> None:
        await started.wait()
        clock[0] += seconds

    async def work() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(task_lease.anyio, "current_time", lambda: clock[0])
    monkeypatch.setattr(task_lease.anyio, "sleep", advance)
    monkeypatch.setattr(task_lease.task_dao, "renew", renew)
    with pytest.raises(task_lease.TaskLeaseLostError):
        await task_lease.run_under_lease(TaskClaim(uuid4(), uuid4()), lambda _guard: work())
    assert cancelled.is_set()
    assert renewals == 2
