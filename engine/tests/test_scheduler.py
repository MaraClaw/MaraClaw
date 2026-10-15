import asyncio
from dataclasses import replace
from datetime import datetime, tzinfo
from typing import Self, override
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

from app.core.logging import get_trace_id, set_trace_id
from app.dao.agent_dao import agent_dao
from app.db import session
from app.records.agent import AgentRecord
from app.services import scheduler, scheduler_tasks
from test_schedule_dao import NEXT, NOW, _next_run, _Pool, _Raw, _schedule, pool as pool
from test_scheduler_tasks import owner as owner


class _Clock(datetime):
    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(NOW.timestamp(), tz)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler, "datetime", _Clock)


async def test_tick_commits_claims_before_detached_launch_and_coalesces_downtime(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given two overdue schedules and a caller trace.
    rows = [_schedule(), replace(_schedule(), instruction="Other report")]
    pool.rows = {row.id: row for row in rows}
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    set_trace_id("caller")
    completed = asyncio.Event()
    launches: list[tuple[UUID, UUID, str]] = []
    tick_traces: list[str | None] = []

    async def execute(schedule_id: UUID, agent_id: UUID, instruction: str) -> None:
        assert session.get_connection() is None
        assert session._completion_ctx.get() is None
        assert get_trace_id() is None
        assert pool.connections[0].commits == 1
        launches.append((schedule_id, agent_id, instruction))
        if len(launches) == len(rows):
            completed.set()

    async def audit(*_args: object, **_kwargs: object) -> None:
        assert session.get_connection() is None
        assert pool.connections[0].commits == 1
        tick_traces.append(get_trace_id())

    monkeypatch.setattr(scheduler, "_execute_schedule", execute)
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
    # When two ticks observe the same overdue occurrences.
    await scheduler._tick()
    first_trace = get_trace_id()
    await scheduler._tick()
    await asyncio.wait_for(completed.wait(), timeout=1)
    # Then each bound row launches once, with slots advanced but counts unchanged.
    assert set(launches) == {(row.id, row.agent_id, row.instruction) for row in rows}
    assert all(pool.rows[row.id] == replace(row, next_run_at=NEXT) for row in rows)
    assert tick_traces == [first_trace] * 3
    assert first_trace is not None
    assert first_trace != "caller"
    assert get_trace_id() not in {"caller", first_trace, None}


async def test_tick_drops_registered_factories_when_commit_fails(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a due claim whose transaction cannot commit.
    row = _schedule()
    pool.rows[row.id] = row
    monkeypatch.setattr(scheduler, "compute_next_run", _next_run)
    commit = AsyncMock(side_effect=RuntimeError("commit failed"))
    monkeypatch.setattr(_Raw, "commit", commit)
    execute = Mock(side_effect=AssertionError("launched on rollback"))
    monkeypatch.setattr(scheduler, "_execute_schedule", execute)
    audit = AsyncMock()
    monkeypatch.setattr("app.services.audit_logger.write_audit_log", audit)
    # When the tick attempts to commit.
    await scheduler._tick()
    # Then rollback discards execution admission and reports the failure.
    commit.assert_awaited_once()
    execute.assert_not_called()
    assert not scheduler_tasks._tasks
    assert not pool.locked
    assert audit.await_args_list[0].args[0] == "schedule_error"


@pytest.mark.parametrize("state", ["missing", "stopped", "expired"])
async def test_execution_skips_without_counting_when_agent_ineligible(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    # Given a persisted schedule and an agent no longer eligible at execution time.
    row = _schedule()
    pool.rows[row.id] = row
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status="running")
    agent.status = "stopped" if state == "stopped" else "running"
    agent.is_expired = state == "expired"
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=None if state == "missing" else agent))
    context = AsyncMock(side_effect=AssertionError("built ineligible context"))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    set_trace_id("request")
    # When execution reloads the agent.
    await scheduler._execute_schedule(row.id, row.agent_id, row.instruction)
    # Then no bookkeeping or LLM context is started, and the skip has its own trace.
    assert pool.rows[row.id] == row
    assert pool.connections == []
    context.assert_not_awaited()
    assert get_trace_id() not in {None, "request"}


@pytest.mark.parametrize("fails", [False, True])
async def test_execution_counts_started_attempt_before_detached_llm_and_retains_history(
    pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    # Given a running agent, with real atomic bookkeeping and an external LLM seam.
    row = _schedule()
    pool.rows[row.id] = row
    agent = AgentRecord(id=row.agent_id, name="Agent", creator_id=row.created_by, status="running")
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    traces: list[str | None] = []

    async def context(*_args: object) -> tuple[str, str]:
        assert session.get_connection() is None
        assert pool.rows[row.id].run_count == row.run_count + 1
        assert pool.connections[0].commits == 1
        traces.append(get_trace_id())
        return "static", "dynamic"

    async def llm(**kwargs: object) -> str:
        assert session.get_connection() is None
        assert kwargs["agent_id"] == row.agent_id
        assert kwargs["session_id"] == str(row.id)
        traces.append(get_trace_id())
        if fails:
            raise RuntimeError("provider failure")
        return "R" * 600

    monkeypatch.setattr("app.services.agent_context.build_agent_context", context)
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", llm)
    activity = AsyncMock()
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    set_trace_id("request")
    # When the eligible execution succeeds or its provider fails.
    await scheduler._execute_schedule(row.id, row.agent_id, row.instruction)
    # Then either result counts one committed attempt without changing the cron slot.
    assert pool.rows[row.id] == replace(row, run_count=row.run_count + 1, last_run_at=NOW)
    assert traces[0] is not None
    assert traces[0] != "request"
    assert traces == [traces[0]] * 2
    if fails:
        activity.assert_not_awaited()
    else:
        assert activity.await_args_list[0].args[:2] == (row.agent_id, "schedule_run")
        assert activity.await_args_list[0].kwargs["detail"] == {
            "schedule_id": str(row.id),
            "instruction": row.instruction,
            "reply": "R" * 500,
        }
