import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.dao.schedule_manual_run_dao import schedule_manual_run_dao as runs
from app.db.session import connection_ctx
from app.records.schedule import AgentScheduleRecord
from app.services import schedule_manual, scheduler_tasks
from app.services.llm.background_guard import BackgroundCallGuard, BackgroundCallOutcome
from app.services.schedule_manual_control import cancel_manual_run
from test_schedule_occurrence_postgres import (
    accept_claim,
    live_schedule as live_schedule,
    manual_schedule as manual_schedule,
)
from test_scheduler_tasks import owner as owner


@pytest.fixture(autouse=True)
def inference(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr(schedule_manual, "build_agent_context", AsyncMock(return_value=("", "")))
    call = AsyncMock(return_value=BackgroundCallOutcome("success", "Finished"))
    monkeypatch.setattr(schedule_manual, "call_agent_llm_with_tools", call)
    return call


async def test_live_restarted_worker_executes_disabled_snapshot_once(
    manual_schedule: AgentScheduleRecord, inference: AsyncMock
) -> None:
    # Given durable acceptance while no worker is alive and later schedule edits.
    accepted = await runs.accept(manual_schedule.id, manual_schedule.created_by, "retry")
    assert accepted is not None
    disabled = await agent_schedule_dao.update(
        db_obj=manual_schedule,
        obj_in={
            "is_enabled": False,
            "instruction": "Future instruction",
        },
    )
    await scheduler_tasks.stop_schedule_tasks()
    scheduler_tasks.start_schedule_tasks()
    # When the next worker generation recovers committed requests.
    await schedule_manual.recover_manual_runs()
    await asyncio.wait_for(asyncio.gather(*tuple(scheduler_tasks._tasks)), timeout=5)
    await schedule_manual.recover_manual_runs()
    retry = await runs.accept(manual_schedule.id, manual_schedule.created_by, "retry")
    # Then exactly one local terminal history and start count reflect the accepted snapshot.
    finished = await runs.get(accepted.id)
    assert finished is not None
    assert finished.outcome == "success"
    assert retry is not None
    assert retry.id == accepted.id
    inference.assert_awaited_once()
    prompt = TypeAdapter(str).validate_python(inference.call_args.kwargs["user_prompt"])
    assert prompt.endswith(manual_schedule.instruction)
    current = await agent_schedule_dao.get(manual_schedule.id)
    assert current is not None
    assert current.run_count == manual_schedule.run_count + 1
    assert current.next_run_at == disabled.next_run_at
    assert current.pending_due_at is None
    history = await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id)
    assert len(history) == 1
    assert history[0].detail_json["instruction"] == manual_schedule.instruction


async def test_live_shutdown_leaves_started_request_recoverable(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a worker blocked inside external inference after first-start bookkeeping.
    entered = asyncio.Event()

    async def block(**_kwargs: object) -> BackgroundCallOutcome:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(schedule_manual, "call_agent_llm_with_tools", block)
    accepted = await runs.accept(manual_schedule.id, manual_schedule.created_by, None)
    assert accepted is not None
    await schedule_manual.recover_manual_runs()
    await asyncio.wait_for(entered.wait(), timeout=5)
    # When shutdown cancels and awaits the owned task before another worker restarts it.
    await scheduler_tasks.stop_schedule_tasks()
    interrupted = await runs.get(accepted.id)
    assert interrupted is not None
    assert interrupted.finished_at is None
    assert interrupted.started_at is not None
    assert not scheduler_tasks._tasks
    assert await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id) == []
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE agent_schedule_manual_runs SET lease_expires_at = %(expired)s WHERE id = %(id)s",
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "id": accepted.id},
        )
    monkeypatch.setattr(
        schedule_manual,
        "call_agent_llm_with_tools",
        AsyncMock(return_value=BackgroundCallOutcome("success", "Recovered")),
    )
    scheduler_tasks.start_schedule_tasks()
    await schedule_manual.recover_manual_runs()
    await asyncio.wait_for(asyncio.gather(*tuple(scheduler_tasks._tasks)), timeout=5)
    # Then restart completes the original receipt without counting its first start twice.
    finished = await runs.get(accepted.id)
    current = await agent_schedule_dao.get(manual_schedule.id)
    assert finished is not None
    assert finished.outcome == "success"
    assert current is not None
    assert current.run_count == manual_schedule.run_count + 1
    assert len(await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id)) == 1


@pytest.mark.parametrize("revoke", ["rotate", "delete"])
async def test_live_worker_stops_after_ownership_revoked_during_inference(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch, revoke: str
) -> None:
    # Given an executing request whose ownership is revoked before guarded dispatch continues.
    claim = await accept_claim(manual_schedule)
    dispatched: list[bool] = []

    async def revoked_call(*, guard: BackgroundCallGuard, **_kwargs: object) -> BackgroundCallOutcome:
        match revoke:
            case "rotate":
                async with connection_ctx() as db:
                    await db.execute(
                        "UPDATE agent_schedule_manual_runs SET lease_token = %(token)s WHERE id = %(id)s",
                        {"token": uuid4(), "id": claim.id},
                    )
            case "delete":
                await agent_schedule_dao.delete(id=manual_schedule.id)
            case _:
                raise AssertionError(revoke)
        await guard.confirm()
        dispatched.append(True)
        return BackgroundCallOutcome("success", "Stale")

    monkeypatch.setattr(schedule_manual, "call_agent_llm_with_tools", revoked_call)
    # When the active worker observes the ownership loss through its tool-loop guard.
    await schedule_manual.execute_manual_run(claim)
    # Then stale execution produces neither another dispatch nor terminal history.
    assert dispatched == []
    assert await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id) == []
    remaining = await runs.get(claim.id)
    assert remaining is None if revoke == "delete" else remaining is not None and remaining.finished_at is None


@pytest.mark.parametrize("revoke", ["membership", "access", "stopped", "expired", "identity", "password"])
async def test_live_recovery_rechecks_requester_and_agent_eligibility(
    manual_schedule: AgentScheduleRecord, inference: AsyncMock, revoke: str
) -> None:
    # Given accepted work followed by an authorization or agent-state change.
    claim = await accept_claim(manual_schedule)
    async with connection_ctx() as db:
        match revoke:
            case "membership":
                await db.execute("UPDATE users SET is_active = FALSE WHERE id = %(id)s", {"id": claim.requester_id})
            case "access":
                other_owner = uuid4()
                await db.execute(
                    "INSERT INTO users (id, display_name) VALUES (%(id)s, 'Other owner')", {"id": other_owner}
                )
                await db.execute(
                    "UPDATE agents SET creator_id = %(owner)s WHERE id = %(id)s",
                    {"owner": other_owner, "id": claim.agent_id},
                )
            case "stopped":
                await db.execute("UPDATE agents SET status = 'stopped' WHERE id = %(id)s", {"id": claim.agent_id})
            case "expired":
                await db.execute("UPDATE agents SET is_expired = TRUE WHERE id = %(id)s", {"id": claim.agent_id})
            case "identity" | "password":
                identity = uuid4()
                await db.execute(
                    "INSERT INTO identities (id, email, is_active, must_change_password) "
                    "VALUES (%(id)s, %(email)s, %(active)s, %(password)s)",
                    {
                        "id": identity,
                        "email": f"{identity}@qa.invalid",
                        "active": revoke != "identity",
                        "password": revoke == "password",
                    },
                )
                await db.execute(
                    "UPDATE users SET identity_id = %(identity)s WHERE id = %(id)s",
                    {"identity": identity, "id": claim.requester_id},
                )
            case _:
                raise AssertionError(revoke)
    # When recovery freshly loads membership, identity, access and agent status.
    await schedule_manual.execute_manual_run(claim)
    # Then ineligible work terminates without inference or first-start accounting.
    inference.assert_not_awaited()
    finished = await runs.get(claim.id)
    assert finished is not None
    assert finished.outcome == "cancelled"
    assert finished.started_at is None
    current = await agent_schedule_dao.get(manual_schedule.id)
    assert current is not None
    assert current.run_count == manual_schedule.run_count
    history = await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id)
    assert len(history) == 1


@pytest.mark.parametrize("failure", ["context", "provider", "result"])
async def test_live_execution_failure_finishes_once_with_history(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # Given accepted work whose context or inference fails after first start.
    claim = await accept_claim(manual_schedule)
    match failure:
        case "context":
            monkeypatch.setattr(schedule_manual, "build_agent_context", AsyncMock(side_effect=RuntimeError("Context")))
        case "provider":
            monkeypatch.setattr(
                schedule_manual, "call_agent_llm_with_tools", AsyncMock(side_effect=RuntimeError("Provider"))
            )
        case "result":
            monkeypatch.setattr(
                schedule_manual,
                "call_agent_llm_with_tools",
                AsyncMock(return_value=BackgroundCallOutcome("failure", "Provider declined")),
            )
        case _:
            raise AssertionError(failure)
    # When execution attempts the original claim twice.
    await schedule_manual.execute_manual_run(claim)
    await schedule_manual.execute_manual_run(claim)
    # Then only the first owner records the failure and increments the start count.
    finished = await runs.get(claim.id)
    assert finished is not None
    assert finished.outcome == "failure"
    current = await agent_schedule_dao.get(manual_schedule.id)
    assert current is not None
    assert current.run_count == manual_schedule.run_count + 1
    history = await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id)
    assert len(history) == 1
    assert history[0].detail_json["status"] == "failure"


async def test_live_terminal_history_failure_rolls_back_then_recovers(
    manual_schedule: AgentScheduleRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a first owner whose terminal history insert fails after inference.
    claim = await accept_claim(manual_schedule)
    with monkeypatch.context() as patch:
        patch.setattr(agent_activity_log_dao, "create", AsyncMock(side_effect=RuntimeError("History insert failed")))
        await schedule_manual.execute_manual_run(claim)
    unfinished = await runs.get(claim.id)
    assert unfinished is not None
    assert unfinished.finished_at is None
    assert await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id) == []
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE agent_schedule_manual_runs SET lease_expires_at = %(expired)s WHERE id = %(id)s",
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "id": claim.id},
        )
    # When a new owner recovers the unfinished receipt.
    recovered = await runs.claim_pending(datetime.now(UTC))
    assert len(recovered) == 1
    await schedule_manual.execute_manual_run(recovered[0])
    # Then terminal state and history commit together without counting another start.
    finished = await runs.get(claim.id)
    assert finished is not None
    assert finished.outcome == "success"
    current = await agent_schedule_dao.get(manual_schedule.id)
    assert current is not None
    assert current.run_count == manual_schedule.run_count + 1
    assert len(await agent_activity_log_dao.list_schedule_history(manual_schedule.agent_id, manual_schedule.id)) == 1


@pytest.mark.parametrize("claimed", [False, True])
async def test_live_cancel_fences_worker(
    manual_schedule: AgentScheduleRecord, inference: AsyncMock, claimed: bool
) -> None:
    # Given accepted work, optionally already handed to a worker but not started.
    run = await runs.accept(manual_schedule.id, manual_schedule.created_by, "cancel-key")
    assert run is not None
    if claimed:
        [run] = await runs.claim_pending(datetime.now(UTC))
    # When cancellation commits before a stale worker or recovery proceeds.
    await cancel_manual_run(run)
    if claimed:
        await schedule_manual.execute_manual_run(run)
    await schedule_manual.recover_manual_runs()
    # Then cancellation is the sole terminal history, with no execution or bookkeeping.
    inference.assert_not_awaited()
    assert await agent_schedule_dao.get(manual_schedule.id) == manual_schedule
    assert len(await agent_activity_log_dao.list_schedule_history(run.agent_id, run.schedule_id)) == 1
