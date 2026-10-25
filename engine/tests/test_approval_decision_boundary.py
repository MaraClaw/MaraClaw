from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import agents
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.approvals import ApprovalStatus
from app.records.agent import AgentRecord
from app.records.audit import ApprovalRequestRecord
from app.records.user import UserRecord
from app.services import autonomy_service as autonomy_module


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [({}, "rejected"), ({"action": "reject"}, "rejected"), ({"action": "approve"}, "approved")],
)
async def test_approval_http_dispatches_closed_decision_and_preserves_default(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, str], expected_status: str
) -> None:
    # Given the real HTTP parser and service, with only persistence replaced.
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=user.id)
    approval = ApprovalRequestRecord(id=uuid4(), agent_id=agent.id, action_type="open-tool-name")
    writes: list[ApprovalStatus] = []

    async def access(current_user: UserRecord, agent_id: UUID) -> tuple[AgentRecord, PermissionAccessLevel]:
        return agent, PermissionAccessLevel.MANAGE

    async def resolve(
        record: ApprovalRequestRecord, *, status: ApprovalStatus, resolved_by: UUID
    ) -> ApprovalRequestRecord:
        writes.append(status)
        record.status = status
        record.resolved_by = resolved_by
        return record

    monkeypatch.setattr(agents, "check_agent_access", access)
    monkeypatch.setattr(autonomy_module.approval_request_dao, "get", AsyncMock(return_value=approval))
    monkeypatch.setattr(autonomy_module.approval_request_dao, "resolve", resolve)
    monkeypatch.setattr(autonomy_module.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(autonomy_module.audit_log_dao, "create", AsyncMock())
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: user
    # When resolving through the public route.
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/agents/{agent.id}/approvals/{approval.id}/resolve", json=payload)
    finally:
        app.dependency_overrides.clear()
    # Then the selected branch reaches persistence and the original wire string survives.
    assert response.status_code == 200
    assert response.json()["status"] == expected_status
    assert writes == [ApprovalStatus(expected_status)]


@pytest.mark.parametrize("action", ["approved", "invalid", "", None, 7])
async def test_unknown_approval_action_is_422_before_access_or_mutation(
    monkeypatch: pytest.MonkeyPatch, action: object
) -> None:
    # Given the real route with observable access and storage seams.
    user = UserRecord(id=uuid4())
    access, read, write = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(agents, "check_agent_access", access)
    monkeypatch.setattr(autonomy_module.approval_request_dao, "get", read)
    monkeypatch.setattr(autonomy_module.approval_request_dao, "resolve", write)
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: user
    # When an unknown action is submitted.
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/agents/{uuid4()}/approvals/{uuid4()}/resolve", json={"action": action})
    finally:
        app.dependency_overrides.clear()
    # Then validation rejects it before even reading the approval or checking access.
    assert response.status_code == 422
    access.assert_not_awaited()
    read.assert_not_awaited()
    write.assert_not_awaited()
