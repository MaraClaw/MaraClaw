from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import background_tasks, tasks
from app.core.security import get_current_user
from app.records.agent import AgentRecord
from app.records.task import TaskClaim, TaskRecord
from app.records.user import UserRecord
from app.services import task_executor, task_lease


@pytest.mark.parametrize("busy", [False, True])
async def test_trigger_claims_before_scheduling_and_returns_conflict(monkeypatch, busy):
    agent_id, user_id, task_id = uuid4(), uuid4(), uuid4()
    task = TaskRecord(id=task_id, agent_id=agent_id, created_by=user_id, title="Ship")
    claim = TaskClaim(task_id, uuid4())
    monkeypatch.setattr(
        tasks,
        "check_agent_access",
        AsyncMock(return_value=(AgentRecord(id=agent_id, name="Agent", creator_id=user_id), "manage")),
    )
    monkeypatch.setattr(tasks.task_dao, "get_for_agent", AsyncMock(return_value=task))
    claimed = AsyncMock(return_value=None if busy else claim)
    monkeypatch.setattr(task_lease, "claim_task", claimed)
    scheduled = []

    def schedule(coroutine, _description):
        assert claimed.await_count == 1
        scheduled.append(coroutine)
        coroutine.close()

    execute = AsyncMock()
    monkeypatch.setattr(task_executor, "execute_task", execute)
    monkeypatch.setattr(background_tasks, "schedule_background_task", schedule)
    app = FastAPI()
    app.include_router(tasks.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=user_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/agents/{agent_id}/tasks/{task_id}/trigger")

    if busy:
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        assert scheduled == []
    else:
        assert response.status_code == 200
        assert response.json() == {"status": "triggered", "task_id": str(task_id)}
        execute.assert_called_once_with(task_id, agent_id, claim)
