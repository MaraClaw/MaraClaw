from collections.abc import Coroutine
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import background_tasks, tasks
from app.core.domain.agents import PermissionAccessLevel
from app.core.security import get_current_user
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
