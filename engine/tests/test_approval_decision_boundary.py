from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal, assert_never
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from tests.transaction_pool_fakes import CountingPool

from app.api import agents, enterprise
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.approvals import ApprovalStatus
from app.db import session
from app.records.agent import AgentRecord
from app.records.audit import ApprovalRequestRecord
from app.records.user import UserRecord
from app.services import autonomy_service as autonomy_module, notification_service
from app.services.agent_tools import ToolParameters


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


type _Route = Literal["agents", "enterprise"]

_FULL_ARGS: ToolParameters = {"path": "reports/q3.md", "content": "line\n" * 300}


@dataclass(frozen=True, slots=True)
class _Held:
    """A held L3 write behind one resolve route, with every side effect observable."""

    app: FastAPI
    url: str
    executions: list[tuple[ToolParameters, int]] = field(default_factory=list[tuple[ToolParameters, int]])
    outcome_audit: AsyncMock = field(default_factory=AsyncMock)
    approval_audit: AsyncMock = field(default_factory=AsyncMock)
    notification: AsyncMock = field(default_factory=AsyncMock)

    async def approve(self) -> Response:
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.post(self.url, json={"action": "approve"})


def _hold(monkeypatch: pytest.MonkeyPatch, route: _Route, *, claims: int, tool: Literal["ok", "boom"] = "ok") -> _Held:
    """Mount ``route`` with real routing and service; ``claims`` conditional writes win before the DAO answers None."""
    user = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Agent", creator_id=user.id)
    pending = ApprovalRequestRecord(
        id=uuid4(),
        agent_id=agent.id,
        action_type="write_workspace_files",
        details={"tool": "write_file", "args": str(_FULL_ARGS)[:200], "requested_by": str(user.id)},
        created_at=datetime.now(UTC),
    )
    claimed = replace(pending, status=ApprovalStatus.APPROVED, resolved_by=user.id, execution_args=_FULL_ARGS)
    wins = iter([claimed] * claims)
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    app = FastAPI()
    match route:
        case "agents":
            app.include_router(agents.router, prefix="/api", dependencies=[Depends(session.bind_crud_connection)])
            app.dependency_overrides[agents.get_current_user] = lambda: user
            url = f"/api/agents/{agent.id}/approvals/{pending.id}/resolve"
        case "enterprise":
            app.include_router(enterprise.router, prefix="/api")
            app.dependency_overrides[enterprise.get_current_user] = lambda: user
            url = f"/api/enterprise/approvals/{pending.id}/resolve"
        case unreachable:
            assert_never(unreachable)
    held = _Held(app, url)

    async def access(current_user: UserRecord, agent_id: UUID) -> tuple[AgentRecord, PermissionAccessLevel]:
        return agent, PermissionAccessLevel.MANAGE

    async def resolve(
        record: ApprovalRequestRecord, *, status: ApprovalStatus, resolved_by: UUID
    ) -> ApprovalRequestRecord | None:
        return next(wins, None)

    async def run_tool(tool_name: str, arguments: ToolParameters, agent_id: UUID) -> str:
        held.executions.append((arguments, pool.raw.commits))
        if tool == "boom":
            raise RuntimeError("boom")
        return "written"

    monkeypatch.setattr(agents, "check_agent_access", access)
    monkeypatch.setattr(autonomy_module.approval_request_dao, "get", AsyncMock(return_value=pending))
    monkeypatch.setattr(autonomy_module.approval_request_dao, "resolve", resolve)
    monkeypatch.setattr(autonomy_module.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(autonomy_module.audit_log_dao, "create", held.approval_audit)
    monkeypatch.setattr(autonomy_module, "write_audit_log", held.outcome_audit)
    monkeypatch.setattr(notification_service, "send_notification", held.notification)
    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", run_tool)
    return held


@pytest.mark.parametrize("route", ["agents", "enterprise"])
async def test_losing_resolver_is_told_it_was_resolved_and_nothing_runs(
    monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    # Given a pending approval that another resolver claims between this caller's read and its conditional write.
    held = _hold(monkeypatch, route, claims=0)
    # When this caller approves.
    response = await held.approve()
    # Then it is refused and neither the tool, the audit trail nor the creator notification is touched.
    assert (response.status_code, response.json()) == (400, {"detail": "Approval already resolved"})
    assert held.executions == []
    held.approval_audit.assert_not_awaited()
    held.outcome_audit.assert_not_awaited()
    held.notification.assert_not_awaited()


@pytest.mark.parametrize("route", ["agents", "enterprise"])
async def test_winning_decision_is_committed_before_the_tool_runs(
    monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    # Given the winning approval on a route with or without a request-wide transaction.
    held = _hold(monkeypatch, route, claims=1)
    # When the approved tool runs.
    response = await held.approve()
    # Then the replay used the complete stored arguments and the claim had already been committed.
    assert response.status_code == 200
    assert held.executions == [(_FULL_ARGS, 1)]


@pytest.mark.parametrize("route", ["agents", "enterprise"])
async def test_failed_execution_stays_approved_is_reported_and_never_reruns(
    monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    # Given an approval whose tool fails on replay; later resolvers lose the claim.
    held = _hold(monkeypatch, route, claims=1, tool="boom")
    # When it is approved and then approved again.
    first = await held.approve()
    second = await held.approve()
    # Then the status stays approved, the outcome is audited and notified, and the tool ran once.
    assert (first.status_code, first.json()["status"]) == (200, "approved")
    assert second.status_code == 400
    assert held.executions == [(_FULL_ARGS, 1)]
    held.outcome_audit.assert_awaited_once()
    outcome = held.outcome_audit.await_args.kwargs
    assert outcome["action"] == "approval_executed"
    assert outcome["details"]["result"] == "Execution failed: boom"
    held.notification.assert_awaited_once()
    assert held.notification.await_args.kwargs["body"] == "Result: Execution failed: boom"
