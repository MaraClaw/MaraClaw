from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.api import agents
from app.core import permissions
from app.core.domain.agents import (
    AgentAccessDecision,
    AgentAccessReason,
    AgentSharingMode,
    PermissionAccessLevel,
    PermissionGrantScope,
)
from app.core.domain.users import UserRole
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.user import UserRecord
from app.services import onboarding

type Policy = tuple[UserRecord, AgentRecord, list[AgentPermissionRecord], AsyncMock, AsyncMock]


@pytest.fixture
def policy(monkeypatch: pytest.MonkeyPatch) -> Policy:
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Shared", creator_id=uuid4(), tenant_id=viewer.tenant_id)
    grants: list[AgentPermissionRecord] = []
    rows = AsyncMock(side_effect=lambda _id: list(grants))
    department = AsyncMock(return_value=True)
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", rows)
    monkeypatch.setattr(permissions.org_department_dao, "has_agent_access", department)
    return viewer, agent, grants, rows, department


@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize("mode", list(AgentSharingMode))
async def test_creator_wins_before_admin_company_and_grants(
    policy: Policy, role: UserRole, mode: AgentSharingMode
) -> None:
    # Given an owner who also matches every later grant.
    viewer, agent, grants, rows, department = policy
    viewer = replace(viewer, role=role)
    agent = replace(agent, creator_id=viewer.id, access_mode=mode)
    grants.append(AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, viewer.id))
    # When resolving the winning decision.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then creator management wins without consulting permission rows or departments.
    assert decision == AgentAccessDecision(PermissionAccessLevel.MANAGE, AgentAccessReason.CREATOR)
    rows.assert_not_awaited()
    department.assert_not_awaited()


@pytest.mark.parametrize("role", [UserRole.ORG_ADMIN, UserRole.PLATFORM_ADMIN])
@pytest.mark.parametrize("mode", [AgentSharingMode.COMPANY, AgentSharingMode.CUSTOM])
async def test_eligible_admin_wins_before_company_and_grants(
    policy: Policy, role: UserRole, mode: AgentSharingMode
) -> None:
    # Given a same-tenant admin with a lower direct grant and department membership.
    viewer, agent, grants, rows, department = policy
    viewer = replace(viewer, role=role)
    agent = replace(agent, access_mode=mode)
    grants.append(AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, viewer.id))
    # When resolving access.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then implicit administrator management wins without inspecting foreign grants.
    assert decision == AgentAccessDecision(PermissionAccessLevel.MANAGE, AgentAccessReason.ADMINISTRATOR)
    rows.assert_not_awaited()
    department.assert_not_awaited()


@pytest.mark.parametrize("level", list(PermissionAccessLevel))
async def test_company_level_wins_before_direct_and_department(policy: Policy, level: PermissionAccessLevel) -> None:
    # Given company sharing with a conflicting direct grant.
    viewer, agent, grants, rows, department = policy
    agent = replace(agent, company_access_level=level)
    grants.append(
        AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, viewer.id, PermissionAccessLevel.MANAGE)
    )
    # When resolving access.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then the company policy supplies both the level and reason.
    assert decision == AgentAccessDecision(level, AgentAccessReason.COMPANY)
    rows.assert_not_awaited()
    department.assert_not_awaited()


@pytest.mark.parametrize("legacy_grant", [False, True])
async def test_company_legacy_fallback_keeps_company_provenance(policy: Policy, legacy_grant: bool) -> None:
    # Given an empty company level with either a legacy row or the default use policy.
    viewer, agent, grants, _rows, department = policy
    agent = replace(agent, company_access_level="")
    if legacy_grant:
        grants.append(
            AgentPermissionRecord(
                uuid4(), agent.id, PermissionGrantScope.COMPANY, access_level=PermissionAccessLevel.MANAGE
            )
        )
    expected = PermissionAccessLevel.MANAGE if legacy_grant else PermissionAccessLevel.USE
    # When resolving the fallback decision.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then both legacy variants preserve their level and company reason.
    assert decision == AgentAccessDecision(expected, AgentAccessReason.COMPANY)
    assert permissions.decide_agent_access(viewer, agent, grants) == expected
    department.assert_not_awaited()


@pytest.mark.parametrize("level", list(PermissionAccessLevel))
async def test_direct_grant_wins_before_department_even_at_use_level(
    policy: Policy, level: PermissionAccessLevel
) -> None:
    # Given custom sharing with both direct and department grants.
    viewer, agent, grants, _rows, department = policy
    agent = replace(agent, access_mode=AgentSharingMode.CUSTOM)
    grants.append(AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, viewer.id, level))
    # When resolving access.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then the direct reason wins for either level, without a department query.
    assert decision == AgentAccessDecision(level, AgentAccessReason.DIRECT)
    assert await permissions.compute_agent_access_level(viewer, agent) == level
    department.assert_not_awaited()


async def test_department_fallback_is_use_only_and_ignores_other_users_grants(policy: Policy) -> None:
    # Given a department member with no matching direct grant.
    viewer, agent, grants, _rows, _department = policy
    agent = replace(agent, access_mode=AgentSharingMode.CUSTOM)
    grants.append(
        AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, uuid4(), PermissionAccessLevel.MANAGE)
    )
    # When resolving access.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then another user's manage grant cannot change the department member's use access.
    assert decision == AgentAccessDecision(PermissionAccessLevel.USE, AgentAccessReason.DEPARTMENT)
    assert permissions.decide_agent_access(viewer, agent, grants) is None


@pytest.mark.parametrize("role", list(UserRole))
async def test_private_agent_does_not_allow_admin_direct_or_department_access(policy: Policy, role: UserRole) -> None:
    # Given a non-owner with every possible grant on a private agent.
    viewer, agent, grants, _rows, department = policy
    viewer = replace(viewer, role=role)
    agent = replace(agent, access_mode=AgentSharingMode.PRIVATE)
    grants.append(AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, viewer.id))
    # When resolving access.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then private mode denies access and does not consult department membership.
    assert decision is None
    department.assert_not_awaited()


@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize("mode", list(AgentSharingMode))
async def test_tenant_check_precedes_creator_admin_company_and_grants(
    policy: Policy, role: UserRole, mode: AgentSharingMode
) -> None:
    # Given a foreign-tenant creator with otherwise matching grants and role.
    viewer, agent, grants, rows, department = policy
    viewer = replace(viewer, tenant_id=uuid4(), role=role)
    agent = replace(agent, creator_id=viewer.id, access_mode=mode)
    grants.append(AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, viewer.id))
    # When resolving access.
    decision = await permissions.compute_agent_access_decision(viewer, agent)
    # Then no winning reason or level survives the tenant boundary.
    assert decision is None
    assert permissions.decide_agent_access(viewer, agent, grants) is None
    rows.assert_not_awaited()
    department.assert_not_awaited()


@dataclass(frozen=True, slots=True)
class Catalog:
    agents: list[AgentRecord]
    grants: dict[UUID, list[AgentPermissionRecord]]
    departments: set[UUID]
    # Agents whose decision needs permission rows for a plain member (all others settle without a query).
    unsettled: set[UUID]
    # Member viewer's winning reason per agent; ``None`` means no access.
    reasons: dict[UUID, AgentAccessReason | None]


@dataclass(frozen=True, slots=True)
class DaoFakes:
    single_rows: AsyncMock
    single_department: AsyncMock
    batch_rows: AsyncMock
    batch_department: AsyncMock


def _catalog(viewer: UserRecord, copies: int = 1) -> Catalog:
    """Every outcome per copy: creator, company (level, legacy row, default), direct, department, denied."""
    now = datetime.now(UTC)
    base = AgentRecord(
        id=uuid4(), name="Shared", creator_id=uuid4(), tenant_id=viewer.tenant_id, created_at=now, updated_at=now
    )
    agents_: list[AgentRecord] = []
    grants: dict[UUID, list[AgentPermissionRecord]] = {}
    departments: set[UUID] = set()
    unsettled: set[UUID] = set()
    reasons: dict[UUID, AgentAccessReason | None] = {}

    def add(
        agent: AgentRecord,
        reason: AgentAccessReason | None,
        rows: Sequence[AgentPermissionRecord] = (),
        *,
        department: bool = False,
        needs_rows: bool = True,
    ) -> None:
        agents_.append(agent)
        grants[agent.id] = list(rows)
        reasons[agent.id] = reason
        if department:
            departments.add(agent.id)
        if needs_rows:
            unsettled.add(agent.id)

    def user_row(agent: AgentRecord, user_id: UUID, level: PermissionAccessLevel) -> AgentPermissionRecord:
        return AgentPermissionRecord(uuid4(), agent.id, PermissionGrantScope.USER, user_id, level)

    for _ in range(copies):
        owned = replace(base, id=uuid4(), creator_id=viewer.id, access_mode=AgentSharingMode.PRIVATE)
        add(owned, AgentAccessReason.CREATOR, needs_rows=False)
        foreign = replace(base, id=uuid4(), creator_id=viewer.id, tenant_id=uuid4())
        add(foreign, None, [user_row(foreign, viewer.id, PermissionAccessLevel.MANAGE)], needs_rows=False)
        for level in PermissionAccessLevel:
            company = replace(base, id=uuid4(), company_access_level=level)
            add(company, AgentAccessReason.COMPANY, [user_row(company, viewer.id, level)], needs_rows=False)
        default = replace(base, id=uuid4(), company_access_level="")
        add(default, AgentAccessReason.COMPANY)
        legacy = replace(base, id=uuid4(), company_access_level="")
        legacy_row = AgentPermissionRecord(
            uuid4(), legacy.id, PermissionGrantScope.COMPANY, access_level=PermissionAccessLevel.MANAGE
        )
        add(legacy, AgentAccessReason.COMPANY, [legacy_row])
        for level in PermissionAccessLevel:
            direct = replace(base, id=uuid4(), access_mode=AgentSharingMode.CUSTOM)
            add(direct, AgentAccessReason.DIRECT, [user_row(direct, viewer.id, level)], department=True)
        via_department = replace(base, id=uuid4(), access_mode=AgentSharingMode.CUSTOM)
        other = user_row(via_department, uuid4(), PermissionAccessLevel.MANAGE)
        add(via_department, AgentAccessReason.DEPARTMENT, [other], department=True)
        no_grants = replace(base, id=uuid4(), access_mode=AgentSharingMode.CUSTOM)
        add(no_grants, None)
        others_only = replace(base, id=uuid4(), access_mode=AgentSharingMode.CUSTOM)
        add(others_only, None, [user_row(others_only, uuid4(), PermissionAccessLevel.MANAGE)])
        private = replace(base, id=uuid4(), access_mode=AgentSharingMode.PRIVATE)
        add(private, None, [user_row(private, viewer.id, PermissionAccessLevel.MANAGE)], department=True)
    return Catalog(agents_, grants, departments, unsettled, reasons)


def _install(monkeypatch: pytest.MonkeyPatch, viewer: UserRecord, catalog: Catalog) -> DaoFakes:
    def batch_rows(agent_ids: Sequence[UUID], user_id: UUID) -> dict[UUID, list[AgentPermissionRecord]]:
        assert user_id == viewer.id
        return {
            agent_id: [
                row
                for row in catalog.grants[agent_id]
                if row.scope_type == PermissionGrantScope.COMPANY
                or (row.scope_type == PermissionGrantScope.USER and row.scope_id == user_id)
            ]
            for agent_id in agent_ids
        }

    fakes = DaoFakes(
        single_rows=AsyncMock(side_effect=lambda agent_id: list(catalog.grants[agent_id])),
        single_department=AsyncMock(side_effect=lambda _user_id, agent_id: agent_id in catalog.departments),
        batch_rows=AsyncMock(side_effect=batch_rows),
        batch_department=AsyncMock(
            side_effect=lambda _user_id, agent_ids: {
                agent_id for agent_id in agent_ids if agent_id in catalog.departments
            }
        ),
    )
    monkeypatch.setattr(permissions.agent_permission_dao, "list_for_agent", fakes.single_rows)
    monkeypatch.setattr(permissions.org_department_dao, "has_agent_access", fakes.single_department)
    monkeypatch.setattr(permissions.agent_permission_dao, "list_decision_rows", fakes.batch_rows)
    monkeypatch.setattr(permissions.org_department_dao, "accessible_agent_ids", fakes.batch_department)
    return fakes


def _forbid_single_path(fakes: DaoFakes) -> None:
    assert fakes.single_rows.await_count == 0
    assert fakes.single_department.await_count == 0


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.ORG_ADMIN, UserRole.PLATFORM_ADMIN])
async def test_batch_matches_single_agent_decision_for_every_outcome(
    monkeypatch: pytest.MonkeyPatch, role: UserRole
) -> None:
    # Given one of every outcome, including grants for other viewers and a legacy company row.
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4(), role=role)
    catalog = _catalog(viewer)
    fakes = _install(monkeypatch, viewer, catalog)
    expected = {agent.id: await permissions.compute_agent_access_decision(viewer, agent) for agent in catalog.agents}
    fakes.single_rows.reset_mock()
    fakes.single_department.reset_mock()
    # When the whole catalog is resolved in one batch.
    decisions = await permissions.compute_agent_access_decisions(viewer, catalog.agents)
    # Then each agent keeps the single-agent level and reason, and the single-agent queries stay unused.
    assert decisions == expected
    _forbid_single_path(fakes)
    reasons = {decision.reason if decision else None for decision in decisions.values()}
    if role == UserRole.MEMBER:
        assert {
            agent.id: (d.reason if (d := decisions[agent.id]) else None) for agent in catalog.agents
        } == catalog.reasons
        assert reasons == {*AgentAccessReason, None} - {AgentAccessReason.ADMINISTRATOR}
    else:
        assert AgentAccessReason.ADMINISTRATOR in reasons


async def test_page_enrichment_excludes_the_continuation_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4())
    catalog = _catalog(viewer, copies=4)
    visible = [agent for agent in catalog.agents if catalog.reasons[agent.id] is not None]
    fakes = _install(monkeypatch, viewer, catalog)
    monkeypatch.setattr(agents, "list_visible_agents", AsyncMock(return_value=visible))
    resets = AsyncMock()
    unread = AsyncMock(return_value={str(visible[0].id): 7})
    onboarded = AsyncMock(return_value={visible[0].id})
    monkeypatch.setattr(agents.agent_dao, "apply_token_counter_resets_many", resets)
    monkeypatch.setattr(agents, "_build_unread_count_by_agent", unread)
    monkeypatch.setattr(onboarding, "onboarded_agent_ids", onboarded)

    page = await agents.list_agents_page(limit=5, cursor=None, current_user=viewer)

    assert [(row.id, row.access_reason) for row in page.items] == [
        (agent.id, catalog.reasons[agent.id]) for agent in visible[:5]
    ]
    assert page.has_more is True
    assert page.next_cursor is not None
    assert (page.items[0].unread_count, page.items[0].onboarded_for_me) == (7, True)
    resets.assert_awaited_once_with(visible[:5])
    unread.assert_awaited_once_with(visible[:5], viewer)
    onboarded.assert_awaited_once_with(viewer.id, [agent.id for agent in visible[:5]])
    assert set(fakes.batch_rows.await_args_list[0].args[0]) <= {agent.id for agent in visible[:5]}
    _forbid_single_path(fakes)


async def test_batch_requests_rows_only_for_agents_that_need_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a mixed catalog for a plain member.
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4())
    catalog = _catalog(viewer, copies=2)
    fakes = _install(monkeypatch, viewer, catalog)
    # When resolving it.
    _ = await permissions.compute_agent_access_decisions(viewer, catalog.agents)
    # Then permission rows are requested once, only for unsettled agents and only for this viewer.
    fakes.batch_rows.assert_awaited_once()
    assert set(fakes.batch_rows.await_args_list[0].args[0]) == catalog.unsettled
    assert fakes.batch_rows.await_args_list[0].args[1] == viewer.id
    # And departments are asked once, only for custom agents without a direct grant.
    fakes.batch_department.assert_awaited_once()
    asked = set(fakes.batch_department.await_args_list[0].args[1])
    assert asked == {
        agent.id
        for agent in catalog.agents
        if agent.access_mode == AgentSharingMode.CUSTOM
        and catalog.reasons[agent.id] in (AgentAccessReason.DEPARTMENT, None)
    }


async def test_batch_without_unsettled_agents_issues_no_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given only agents that creator, administrator or company level settle.
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4())
    catalog = _catalog(viewer, copies=3)
    settled = [agent for agent in catalog.agents if agent.id not in catalog.unsettled]
    fakes = _install(monkeypatch, viewer, catalog)
    # When resolving them, and an empty list.
    decisions = await permissions.compute_agent_access_decisions(viewer, settled)
    empty = await permissions.compute_agent_access_decisions(viewer, [])
    # Then no row or department query runs at all.
    assert {agent.id for agent in settled} == decisions.keys()
    assert empty == {}
    fakes.batch_rows.assert_not_awaited()
    fakes.batch_department.assert_not_awaited()
    _forbid_single_path(fakes)


@pytest.mark.parametrize("copies", [1, 5, 40])
async def test_batch_query_count_is_constant_as_the_catalog_grows(monkeypatch: pytest.MonkeyPatch, copies: int) -> None:
    # Given catalogs of growing size.
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4())
    catalog = _catalog(viewer, copies=copies)
    fakes = _install(monkeypatch, viewer, catalog)
    # When resolving the whole catalog.
    decisions = await permissions.compute_agent_access_decisions(viewer, catalog.agents)
    # Then exactly one rows query and one department query run, however many agents there are.
    assert len(decisions) == len(catalog.agents)
    assert (fakes.batch_rows.await_count, fakes.batch_department.await_count) == (1, 1)
    _forbid_single_path(fakes)


async def test_list_route_resolves_every_decision_in_one_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a visible catalog served through the real batch decision and fake DAOs.
    viewer = UserRecord(id=uuid4(), tenant_id=uuid4())
    catalog = _catalog(viewer, copies=4)
    visible = [agent for agent in catalog.agents if catalog.reasons[agent.id] is not None]
    fakes = _install(monkeypatch, viewer, catalog)
    monkeypatch.setattr(agents, "list_visible_agents", AsyncMock(return_value=visible))
    monkeypatch.setattr(agents.agent_dao, "apply_token_counter_resets_many", AsyncMock())
    monkeypatch.setattr(agents, "_build_unread_count_by_agent", AsyncMock(return_value={}))
    monkeypatch.setattr(onboarding, "onboarded_agent_ids", AsyncMock(return_value=set()))
    # When the member lists agents.
    listed = await agents.list_agents(limit=500, current_user=viewer)
    # Then each row carries its own reason and the per-agent queries never run.
    assert [(row.id, row.access_reason) for row in listed] == [(a.id, catalog.reasons[a.id]) for a in visible]
    assert (fakes.batch_rows.await_count, fakes.batch_department.await_count) == (1, 1)
    _forbid_single_path(fakes)
