from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
