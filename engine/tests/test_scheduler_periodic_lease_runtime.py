import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.domain.agents import AgentStatus
from app.core.logging import get_trace_id, set_trace_id
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.agent_dao import agent_dao
from app.dao.schedule_dao import agent_schedule_dao
from app.db import session
from app.records.agent import AgentRecord
from app.records.schedule import AgentScheduleRecord
from app.services import schedule_lease, scheduler, scheduler_tasks
from app.services.llm.background_guard import BackgroundCallOutcome
from test_schedule_dao import NEXT, NOW, _next_run, _Pool, _schedule, pool
from test_scheduler import _Clock
from test_scheduler_tasks import owner

__all__ = ["owner", "pool"]


@pytest.fixture
async def occurrence(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> AgentScheduleRecord:
    row = _schedule()
    pool.rows[row.id] = row
    (claim,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    monkeypatch.setattr(scheduler, "datetime", _Clock)
    monkeypatch.setattr(schedule_lease, "datetime", _Clock)
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status=AgentStatus.RUNNING)
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("static", "dynamic")))
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools", AsyncMock(return_value=BackgroundCallOutcome("success", "reply"))
    )
    return claim


@pytest.fixture
def activities(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []

    async def create(*, obj_in: dict[str, object]) -> None:
        assert session.get_connection() is not None
        assert "pending_due_at = NULL" in pool.connections[-1].executed[-1][0]
        assert not any("ScheduleLeaseHold._heartbeat" in str(task.get_coro()) for task in asyncio.all_tasks())
        entries.append(obj_in)

    monkeypatch.setattr(agent_activity_log_dao, "create", create)
    return entries


@pytest.mark.parametrize("result", ["success", "provider_failure", "provider_exception", "context_exception"])
async def test_periodic_terminal_outcomes_are_fenced_without_llm_transaction(
    occurrence, pool, activities, monkeypatch, result
):
    traces = []

    async def context(*_args):
        assert session.get_connection() is None
        assert pool.rows[occurrence.id].run_count == occurrence.run_count + 1
        traces.append(get_trace_id())
        if result == "context_exception":
            raise RuntimeError("context failure")
        return "static", "dynamic"

    async def llm(**kwargs):
        assert session.get_connection() is None
        assert kwargs["session_id"] == str(occurrence.id)
        await kwargs["guard"].confirm()
        traces.append(get_trace_id())
        if result == "provider_exception":
            raise RuntimeError("provider failure")
        return BackgroundCallOutcome("failure" if result == "provider_failure" else "success", "R" * 600)

    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", llm)
    set_trace_id("request")
    await scheduler._execute_periodic_schedule(occurrence)
    row = pool.rows[occurrence.id]
    assert row.pending_due_at is None
    assert row.lease_token is None
    assert row.run_count == occurrence.run_count + 1
    assert row.last_run_at == NOW
    assert row.next_run_at == NEXT
    assert len(activities) == 1
    assert activities[0]["detail_json"]["status"] == ("success" if result == "success" else "failure")
    assert activities[0]["detail_json"]["instruction"] == occurrence.pending_instruction
    assert traces
    assert traces[0] not in {None, "request"}
    assert len(set(traces)) == 1
    assert all(raw.commits == 1 for raw in pool.connections)


@pytest.mark.parametrize("state", ["missing", "stopped", "expired"])
async def test_periodic_ineligible_agent_terminally_skips_before_count(
    occurrence, pool, activities, monkeypatch, state
):
    agent = AgentRecord(
        id=occurrence.agent_id,
        name="Agent",
        creator_id=occurrence.created_by,
        status=AgentStatus.STOPPED if state == "stopped" else AgentStatus.RUNNING,
        is_expired=state == "expired",
    )
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=None if state == "missing" else agent))
    context = AsyncMock(side_effect=AssertionError("ineligible context"))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    await scheduler._execute_periodic_schedule(occurrence)
    assert pool.rows[occurrence.id].pending_due_at is None
    assert pool.rows[occurrence.id].run_count == occurrence.run_count
    assert activities[0]["detail_json"]["status"] == "skipped"
    context.assert_not_awaited()


async def test_periodic_recovery_retains_snapshot_and_counts_only_first_start(occurrence, pool, activities):
    assert occurrence.lease_token is not None
    await agent_schedule_dao.start_occurrence(occurrence.id, occurrence.lease_token, NOW)
    pool.rows[occurrence.id] = replace(
        pool.rows[occurrence.id], instruction="Edited instruction", lease_expires_at=NOW - timedelta(seconds=1)
    )
    (reclaimed,) = await agent_schedule_dao.claim_due(NOW, _next_run)
    await scheduler._execute_periodic_schedule(occurrence)
    assert activities == []
    await scheduler._execute_periodic_schedule(reclaimed)
    assert pool.rows[occurrence.id].run_count == occurrence.run_count + 1
    assert pool.rows[occurrence.id].next_run_at == NEXT
    assert activities[0]["detail_json"]["instruction"] == occurrence.pending_instruction


@pytest.mark.parametrize("stage", ["context", "provider"])
async def test_periodic_shutdown_leaves_started_slot_recoverable(occurrence, pool, activities, monkeypatch, stage):
    entered = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    target = (
        "app.services.agent_context.build_agent_context"
        if stage == "context"
        else "app.services.llm.call_agent_llm_with_tools"
    )
    monkeypatch.setattr(target, blocked)
    scheduler_tasks.schedule_execution(lambda: scheduler._execute_periodic_schedule(occurrence))
    await asyncio.wait_for(entered.wait(), 1)
    (task,) = scheduler_tasks._tasks
    await scheduler_tasks.stop_schedule_tasks()
    assert task.cancelled()
    assert not scheduler_tasks._tasks
    assert activities == []
    row = pool.rows[occurrence.id]
    assert row.pending_due_at == occurrence.pending_due_at
    assert row.pending_started_at == NOW
    assert row.run_count == occurrence.run_count + 1
    (reclaimed,) = await agent_schedule_dao.claim_due(NOW + timedelta(minutes=6), _next_run)
    assert reclaimed.pending_due_at == occurrence.pending_due_at
    assert reclaimed.pending_started_at == NOW
    assert reclaimed.next_run_at == NEXT
    assert reclaimed.run_count == row.run_count
    assert not any("ScheduleLeaseHold._heartbeat" in str(task.get_coro()) for task in asyncio.all_tasks())


@pytest.mark.parametrize("stage", ["context", "provider"])
@pytest.mark.parametrize("result", ["success", "failure"])
async def test_periodic_revocation_during_await_prevents_terminal_history(
    occurrence, pool, activities, monkeypatch, stage, result
):
    async def revoked(*_args, **_kwargs):
        pool.rows[occurrence.id] = replace(pool.rows[occurrence.id], lease_token=uuid4())
        await asyncio.sleep(0)
        if result == "failure":
            raise RuntimeError("late failure")
        return ("static", "dynamic") if stage == "context" else BackgroundCallOutcome("success", "late reply")

    target = (
        "app.services.agent_context.build_agent_context"
        if stage == "context"
        else "app.services.llm.call_agent_llm_with_tools"
    )
    monkeypatch.setattr(target, revoked)
    await scheduler._execute_periodic_schedule(occurrence)
    assert activities == []
    assert pool.rows[occurrence.id].pending_due_at == occurrence.pending_due_at
    assert pool.rows[occurrence.id].lease_token != occurrence.lease_token


@pytest.mark.parametrize("status", ["success", "failure"])
async def test_periodic_stale_finish_cannot_publish_any_outcome(occurrence, pool, activities, monkeypatch, status):
    original = agent_schedule_dao.finish_occurrence

    async def finish(schedule_id, token, now):
        pool.rows[schedule_id] = replace(pool.rows[schedule_id], lease_token=uuid4())
        return await original(schedule_id, token, now)

    monkeypatch.setattr(agent_schedule_dao, "finish_occurrence", finish)
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools",
        AsyncMock(return_value=BackgroundCallOutcome(status, "late reply")),
    )
    await scheduler._execute_periodic_schedule(occurrence)
    assert activities == []
    assert pool.rows[occurrence.id].pending_due_at == occurrence.pending_due_at


async def test_periodic_activity_error_rolls_back_slot_clear(occurrence, pool, monkeypatch):
    activity = AsyncMock(side_effect=RuntimeError("activity write failed"))
    monkeypatch.setattr(agent_activity_log_dao, "create", activity)
    await scheduler._execute_periodic_schedule(occurrence)
    activity.assert_awaited_once()
    assert pool.rows[occurrence.id].pending_due_at == occurrence.pending_due_at
    assert pool.rows[occurrence.id].lease_token == occurrence.lease_token


async def test_tick_recovers_committed_occurrence_after_launch_was_lost(occurrence, pool, activities, monkeypatch):
    pool.rows[occurrence.id] = replace(
        occurrence, instruction="Edited after crash", lease_expires_at=NOW - timedelta(seconds=1)
    )
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", AsyncMock())
    compute = AsyncMock(side_effect=AssertionError("advanced cron during recovery"))
    monkeypatch.setattr(scheduler, "compute_next_run", compute)
    await scheduler._tick()
    await asyncio.gather(*scheduler_tasks._tasks)
    assert len(activities) == 1
    assert activities[0]["detail_json"]["instruction"] == occurrence.pending_instruction
    assert activities[0]["detail_json"]["due_at"] == occurrence.pending_due_at.isoformat()
    assert pool.rows[occurrence.id].run_count == occurrence.run_count + 1
    assert pool.rows[occurrence.id].next_run_at == NEXT
    compute.assert_not_called()


async def test_periodic_heartbeat_revocation_cancels_blocked_provider_without_history(
    occurrence, pool, activities, monkeypatch
):
    cancelled = asyncio.Event()

    async def llm(**_kwargs):
        pool.rows[occurrence.id] = replace(pool.rows[occurrence.id], lease_token=uuid4())
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(schedule_lease, "LEASE_RENEW_INTERVAL", timedelta(milliseconds=1))
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", llm)
    await asyncio.wait_for(scheduler._execute_periodic_schedule(occurrence), 1)
    assert cancelled.is_set()
    assert activities == []
    assert pool.rows[occurrence.id].pending_due_at == occurrence.pending_due_at
