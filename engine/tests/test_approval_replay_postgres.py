import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, assert_never
from uuid import UUID, uuid4

import anyio
import pytest
from anyio import Path
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import agents, enterprise
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.dao.agent_dao import agent_dao
from app.dao.approval_dao import approval_request_dao
from app.db import session
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.records.agent import AgentRecord
from app.records.audit import ApprovalRequestRecord
from app.records.user import UserRecord
from app.schemas.schemas import ApprovalRequestOut
from app.services import notification_service
from app.services.agent_tools import ToolParameters
from app.services.autonomy_service import autonomy_service

type _Route = Literal["agents", "enterprise"]


@dataclass(frozen=True, slots=True)
class _World:
    db: DbConnection
    user: UserRecord
    agent: AgentRecord
    args: ToolParameters

    async def hold(self) -> ApprovalRequestRecord:
        """Create the pending approval an L3 write leaves behind: bounded details plus the private full arguments."""
        return await approval_request_dao.create_pending(
            agent_id=self.agent.id,
            action_type="write_workspace_files",
            details={"tool": "write_file", "args": str(self.args)[:200], "requested_by": str(self.user.id)},
            execution_args=self.args,
        )

    async def status(self, approval: ApprovalRequestRecord) -> str:
        value = await self.db.fetchval("SELECT status FROM approval_requests WHERE id = %(id)s", {"id": approval.id})
        assert isinstance(value, str)
        return value

    def app(self, route: _Route) -> FastAPI:
        app = FastAPI()
        match route:
            case "agents":
                app.include_router(agents.router, prefix="/api", dependencies=[Depends(session.bind_crud_connection)])
                app.dependency_overrides[agents.get_current_user] = lambda: self.user
            case "enterprise":
                app.include_router(enterprise.router, prefix="/api")
                app.dependency_overrides[enterprise.get_current_user] = lambda: self.user
            case unreachable:
                assert_never(unreachable)
        return app

    async def approve(self, route: _Route, approval: ApprovalRequestRecord) -> Response:
        url = {
            "agents": f"/api/agents/{self.agent.id}/approvals/{approval.id}/resolve",
            "enterprise": f"/api/enterprise/approvals/{approval.id}/resolve",
        }[route]
        transport = ASGITransport(app=self.app(route), raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(url, json={"action": "approve"})


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL approval QA")
    schema = f"approval_replay_qa_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(await Path("scripts/schema_baseline.sql").read_text())
            _ = await init_pool(
                conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=8
            )
            tenant_id, user_id, agent_id = uuid4(), uuid4(), uuid4()
            await db.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%(tenant)s, 'Acme', 'acme')", {"tenant": tenant_id}
            )
            await db.execute(
                "INSERT INTO users (id, tenant_id, display_name) VALUES (%(user)s, %(tenant)s, 'Manager')",
                {"user": user_id, "tenant": tenant_id},
            )
            await db.execute(
                "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, "
                "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
                "heartbeat_interval_minutes) VALUES (%(agent)s, 'Agent', 'Writer', %(user)s, %(tenant)s, 1, 1, 1, "
                "'company', 'use', 1)",
                {"agent": agent_id, "user": user_id, "tenant": tenant_id},
            )
            agent = await agent_dao.get(agent_id)
            assert agent is not None

            async def access(current_user: UserRecord, requested: UUID) -> tuple[AgentRecord, PermissionAccessLevel]:
                return agent, PermissionAccessLevel.MANAGE

            monkeypatch.setattr(agents, "check_agent_access", access)
            yield _World(
                db, UserRecord(id=user_id, tenant_id=tenant_id), agent, {"path": "q3.md", "content": "row\n" * 800}
            )
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def test_list_and_get_queries_never_load_the_full_arguments(world: _World) -> None:
    # Given a stored approval whose complete arguments exceed the 200-character summary.
    approval = await world.hold()
    stored = await world.db.fetchval(
        "SELECT execution_args FROM approval_requests WHERE id = %(id)s", {"id": approval.id}
    )
    assert stored == world.args
    # When the shared read paths load it.
    loaded = [
        await approval_request_dao.get(approval.id),
        *await approval_request_dao.list_for_agent(world.agent.id),
        *await approval_request_dao.list_scoped(tenant_id=world.user.tenant_id),
    ]
    # Then none of them carries the private arguments.
    assert [record.execution_args for record in loaded if record is not None] == [None, None, None]


async def test_only_the_claim_returns_the_full_arguments_and_no_surface_serializes_them(world: _World) -> None:
    # Given a pending approval.
    approval = await world.hold()
    # When it is claimed.
    claimed = await approval_request_dao.resolve(approval, status=ApprovalStatus.APPROVED, resolved_by=world.user.id)
    # Then the claim replays the exact object while the response model, repr and summary stay bounded.
    assert claimed is not None
    assert claimed.execution_args == world.args
    assert "row\n" * 100 not in repr(claimed)
    assert "execution_args" not in ApprovalRequestOut.model_validate(claimed).model_dump()
    assert len(str(claimed.details["args"])) <= 200


async def test_concurrent_claims_have_exactly_one_winner(world: _World) -> None:
    # Given a pending approval and eight resolvers racing with mixed decisions.
    approval = await world.hold()
    outcomes: list[ApprovalRequestRecord | None] = []

    async def claim(status: ApprovalStatus) -> None:
        outcomes.append(await approval_request_dao.resolve(approval, status=status, resolved_by=world.user.id))

    # When they all attempt the transition.
    async with anyio.create_task_group() as tasks:
        for index in range(8):
            tasks.start_soon(claim, ApprovalStatus.APPROVED if index % 2 else ApprovalStatus.REJECTED)
    # Then one claim is returned, the others get nothing, and the row holds the winner's decision.
    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1
    assert await world.status(approval) == winners[0].status.value


async def test_resolver_holding_a_stale_pending_read_loses_the_claim(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a resolver that read the approval as pending before another resolver committed.
    approval = await world.hold()
    stale = await approval_request_dao.get(approval.id)
    assert stale is not None
    _ = await approval_request_dao.resolve(approval, status=ApprovalStatus.REJECTED, resolved_by=world.user.id)

    async def read_before_commit(approval_id: UUID) -> ApprovalRequestRecord | None:
        return stale

    executed: list[ToolParameters] = []

    async def run_tool(tool_name: str, arguments: ToolParameters, agent_id: UUID) -> str:
        executed.append(arguments)
        return "written"

    monkeypatch.setattr(approval_request_dao, "get", read_before_commit)
    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", run_tool)
    # When it tries to approve.
    with pytest.raises(ValueError, match="Approval already resolved"):
        _ = await autonomy_service.resolve_approval(approval.id, world.user, ApprovalAction.APPROVE)
    # Then the tool never runs and the first decision stands.
    assert executed == []
    assert await world.status(approval) == "rejected"


@pytest.mark.parametrize("route", ["agents", "enterprise"])
async def test_winner_is_committed_while_its_tool_runs_and_a_concurrent_loser_is_refused(
    world: _World, monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    # Given a tool that stays running until released.
    approval = await world.hold()
    started, release = anyio.Event(), anyio.Event()
    executed: list[ToolParameters] = []

    async def run_tool(tool_name: str, arguments: ToolParameters, agent_id: UUID) -> str:
        executed.append(arguments)
        started.set()
        await release.wait()
        return "written"

    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", run_tool)
    responses: dict[str, Response] = {}

    async def approve(name: str) -> None:
        responses[name] = await world.approve(route, approval)

    visible_status = ""
    # When a second approval arrives while the first approver's tool is still running.
    with anyio.fail_after(30):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(approve, "winner")
            await started.wait()
            visible_status = await world.status(approval)
            await approve("loser")
            release.set()
    # Then the decision was already visible to other sessions, the loser was refused, and the tool ran once.
    assert visible_status == "approved"
    assert (responses["winner"].status_code, responses["winner"].json()["status"]) == (200, "approved")
    assert (responses["loser"].status_code, responses["loser"].json()) == (400, {"detail": "Approval already resolved"})
    assert executed == [world.args]


@pytest.mark.parametrize("route", ["agents", "enterprise"])
async def test_failed_execution_stays_approved_with_outcome_in_audit_and_notification(
    world: _World, monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    # Given a tool that fails on replay.
    approval = await world.hold()
    executed: list[ToolParameters] = []

    async def failing_tool(tool_name: str, arguments: ToolParameters, agent_id: UUID) -> str:
        executed.append(arguments)
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", failing_tool)
    # When the approval is approved and then approved again.
    first = await world.approve(route, approval)
    second = await world.approve(route, approval)
    # Then it stays approved, the outcome is recorded once, and the tool is not run again.
    assert (first.status_code, second.status_code) == (200, 400)
    assert executed == [world.args]
    assert await world.status(approval) == "approved"
    outcome = await world.db.fetchall(
        "SELECT details->>'result' AS result FROM audit_logs WHERE agent_id = %(agent)s AND action = 'approval_executed'",
        {"agent": world.agent.id},
    )
    assert outcome == [{"result": "Execution failed: boom"}]
    notified = await world.db.fetchall("SELECT body FROM notifications WHERE ref_id = %(id)s", {"id": approval.id})
    assert notified == [{"body": "Result: Execution failed: boom"}]


@pytest.mark.parametrize("route", ["agents", "enterprise"])
async def test_request_failure_after_execution_cannot_revert_the_decision(
    world: _World, monkeypatch: pytest.MonkeyPatch, route: _Route
) -> None:
    # Given a tool that succeeds and a notification step that then fails the request.
    approval = await world.hold()
    executed: list[ToolParameters] = []

    async def run_tool(tool_name: str, arguments: ToolParameters, agent_id: UUID) -> str:
        executed.append(arguments)
        return "written"

    async def notification_down(**kwargs: object) -> None:
        raise RuntimeError("notification down")

    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", run_tool)
    monkeypatch.setattr(notification_service, "send_notification", notification_down)
    # When the approver's request fails after the tool ran, and the approval is approved again.
    first = await world.approve(route, approval)
    second = await world.approve(route, approval)
    # Then the decision and its execution record survive, and the retry is refused without rerunning the tool.
    assert (first.status_code, second.status_code) == (500, 400)
    assert await world.status(approval) == "approved"
    assert executed == [world.args]
    actions = await world.db.fetchall("SELECT action FROM audit_logs WHERE agent_id = %(a)s", {"a": world.agent.id})
    assert {row["action"] for row in actions} == {"approval_approved", "approval_executed"}
