from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import background_tasks, tasks
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.tasks import TaskStatus
from app.core.security import get_current_user
from app.dao.task_dao import TaskDAO
from app.records.agent import AgentRecord
from app.records.task import TaskClaim, TaskRecord
from app.records.user import UserRecord
from app.services import task_executor, task_lease


async def test_trigger_claims_before_scheduling_and_returns_conflict_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Alex", creator_id=user.id)
    task = TaskRecord(id=uuid4(), agent_id=agent.id, title="Report", created_by=user.id)
    owner = TaskClaim(task.id, uuid4())
    claim = AsyncMock(side_effect=[owner, None])
    execute = AsyncMock()
    scheduled: list[str] = []

    def schedule(work: Coroutine[object, object, None], name: str) -> None:
        assert claim.await_count == 1
        scheduled.append(name)
        work.close()

    monkeypatch.setattr(tasks, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.MANAGE)))
    monkeypatch.setattr(tasks.task_dao, "get_for_agent", AsyncMock(return_value=task))
    monkeypatch.setattr(task_lease, "claim_task", claim)
    monkeypatch.setattr(task_executor, "execute_task", execute)
    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    app = FastAPI()
    app.include_router(tasks.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/api/agents/{agent.id}/tasks/{task.id}/trigger")
        second = await client.post(f"/api/agents/{agent.id}/tasks/{task.id}/trigger")
    assert first.status_code == 200
    assert first.json() == {"status": "triggered", "task_id": str(task.id)}
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    assert scheduled == ["execute task"]
    execute.assert_called_once_with(task.id, agent.id, owner)


async def test_missing_task_is_404_without_claim_or_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Alex", creator_id=user.id)
    claim = AsyncMock()
    monkeypatch.setattr(tasks, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.MANAGE)))
    monkeypatch.setattr(tasks.task_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(task_lease, "claim_task", claim)
    app = FastAPI()
    app.include_router(tasks.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/tasks/{uuid4()}/trigger")
    assert response.status_code == 404
    claim.assert_not_awaited()


def _created_task(user: UserRecord, agent: AgentRecord) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(id=uuid4(), agent_id=agent.id, title="Report", created_by=user.id, created_at=now, updated_at=now)


def _task_app(monkeypatch: pytest.MonkeyPatch, user: UserRecord, agent: AgentRecord) -> FastAPI:
    monkeypatch.setattr(tasks, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.MANAGE)))
    monkeypatch.setattr(tasks.user_dao, "get_with_identity", AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(tasks.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return app


async def test_create_claims_a_todo_before_scheduling_and_runs_under_that_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Alex", creator_id=user.id)
    task = _created_task(user, agent)
    owner = TaskClaim(task.id, uuid4())
    claim = AsyncMock(return_value=owner)
    execute = AsyncMock()
    scheduled: list[str] = []

    def schedule(work: Coroutine[object, object, None], name: str) -> None:
        assert claim.await_count == 1
        scheduled.append(name)
        work.close()

    monkeypatch.setattr(tasks.task_dao, "create", AsyncMock(return_value=task))
    monkeypatch.setattr(task_lease, "claim_task", claim)
    monkeypatch.setattr(task_executor, "execute_task", execute)
    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    app = _task_app(monkeypatch, user, agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/tasks/", json={"title": "Report"})
    assert response.status_code == 201
    claim.assert_awaited_once_with(task.id, agent.id)
    execute.assert_called_once_with(task.id, agent.id, owner)
    assert scheduled == ["execute task"]


async def test_create_leaves_supervision_tasks_unclaimed_and_unscheduled(monkeypatch: pytest.MonkeyPatch) -> None:
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Alex", creator_id=user.id)
    task = _created_task(user, agent)
    claim = AsyncMock()
    schedule = AsyncMock()
    monkeypatch.setattr(tasks.task_dao, "create", AsyncMock(return_value=task))
    monkeypatch.setattr(task_lease, "claim_task", claim)
    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    app = _task_app(monkeypatch, user, agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/agents/{agent.id}/tasks/", json={"title": "Check in", "type": "supervision"}
        )
    assert response.status_code == 201
    claim.assert_not_awaited()
    schedule.assert_not_called()


async def test_lost_start_is_returned_to_pending_by_recovery_and_never_rerun(monkeypatch: pytest.MonkeyPatch) -> None:
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Alex", creator_id=user.id)
    task = _created_task(user, agent)
    logs: list[str] = []

    async def claim(task_id: object, agent_id: object) -> TaskClaim:
        task.claim_token, task.status = uuid4(), TaskStatus.DOING
        return TaskClaim(task.id, task.claim_token)

    async def recover(*, limit: int, log: str) -> list[object]:
        assert limit > 0
        if task.claim_token is None:
            return []
        task.claim_token, task.status = None, TaskStatus.PENDING
        logs.append(log)
        return [task.id]

    execute = AsyncMock()
    monkeypatch.setattr(background_tasks, "_accepting", False)  # shutdown rejects the scheduled run
    monkeypatch.setattr(tasks.task_dao, "create", AsyncMock(return_value=task))
    monkeypatch.setattr(task_lease, "claim_task", claim)
    monkeypatch.setattr(task_lease.task_dao, "recover_expired", recover)
    monkeypatch.setattr(task_executor, "execute_task", execute)
    app = _task_app(monkeypatch, user, agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent.id}/tasks/", json={"title": "Report"})
    assert response.status_code == 201
    assert task.status == TaskStatus.DOING
    assert await task_lease.recover_expired_leases() == 1
    assert task.status == TaskStatus.PENDING
    assert logs == [task_lease.RECOVERY_LOG]
    execute.assert_not_awaited()


class _RecordingDb:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.statements: list[str] = []

    async def fetchone(self, sql: str, _params: object) -> dict[str, object]:
        self.statements.append(sql)
        return self.row


@pytest.mark.parametrize(("body", "revokes"), [({"status": "pending"}, True), ({"title": "Renamed"}, False)])
async def test_patch_revokes_the_claim_only_when_it_writes_a_status(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, str], revokes: bool
) -> None:
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Alex", creator_id=user.id)
    task = _created_task(user, agent)
    db = _RecordingDb(asdict(task))

    @asynccontextmanager
    async def session() -> AsyncGenerator[_RecordingDb]:
        yield db

    monkeypatch.setattr(TaskDAO, "session", lambda _self: session())
    monkeypatch.setattr(tasks.task_dao, "get_for_agent", AsyncMock(return_value=task))
    app = _task_app(monkeypatch, user, agent)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(f"/api/agents/{agent.id}/tasks/{task.id}", json=body)
    assert response.status_code == 200
    assert len(db.statements) == 1
    assert ("claim_token" in db.statements[0].split(" RETURNING ")[0]) is revokes
