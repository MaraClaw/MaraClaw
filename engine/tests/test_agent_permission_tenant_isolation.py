from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import agents
from app.core.domain.agents import PermissionAccessLevel, PermissionGrantScope
from app.core.domain.users import UserRole
from app.core.permissions import compute_agent_access_level
from app.records.agent import AgentPermissionRecord, AgentRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from test_agent_create_departments import creation

__all__ = ["creation"]


@pytest.fixture
def permission_directory(creation, monkeypatch):
    store, client = creation
    owner = store.actor
    agent = AgentRecord(id=uuid4(), name="Legacy grants", creator_id=owner.id, tenant_id=owner.tenant_id)
    member = UserRecord(id=uuid4(), tenant_id=owner.tenant_id, display_name="Valid member")
    admin = UserRecord(id=uuid4(), tenant_id=owner.tenant_id, role=UserRole.ORG_ADMIN, display_name="Company admin")
    foreign = UserRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        display_name="Foreign identity",
        identity=IdentityRecord(id=uuid4(), email="foreign-private@example.test"),
    )
    users = {user.id: user for user in (owner, member, admin, foreign)}

    async def authorize(current_user, _agent_id):
        level = await compute_agent_access_level(current_user, agent)
        if level is None:
            raise HTTPException(403, "No access to this agent")
        return agent, level

    async def get_users(ids):
        return {user_id: users[user_id] for user_id in ids if user_id in users}

    monkeypatch.setattr(agents, "check_agent_access", authorize)
    monkeypatch.setattr(agents.agent_permission_dao, "list_for_agent", AsyncMock(return_value=store.grants))
    monkeypatch.setattr(agents.user_dao, "get_many_with_identity", get_users)
    monkeypatch.setattr(agents.user_dao, "list_active_admins_for_tenant", AsyncMock(return_value=[admin]))
    return store, client, agent, owner, member, admin, foreign


@pytest.mark.parametrize("access_mode", ["company", "private", "custom"])
@pytest.mark.parametrize("foreign_role", ["member", "org_admin", "platform_admin"])
async def test_permissions_hide_legacy_foreign_and_missing_user_ids(permission_directory, access_mode, foreign_role):
    # Given legacy mixed grants, including a foreign user whose admin role cannot bypass tenancy.
    store, client, agent, _owner, member, _admin, foreign = permission_directory
    agent.access_mode = access_mode
    foreign.role = foreign_role
    missing_id = uuid4()
    store.grants.extend(
        AgentPermissionRecord(
            id=uuid4(),
            agent_id=agent.id,
            scope_type=PermissionGrantScope.USER,
            scope_id=user_id,
            access_level=PermissionAccessLevel.USE,
        )
        for user_id in (member.id, foreign.id, missing_id)
    )

    # When an authorized owner reads permissions over HTTP.
    response = await client.get(f"/api/agents/{agent.id}/permissions")

    # Then neither user IDs nor identities from invalid grants are disclosed.
    assert response.status_code == 200
    body = response.json()
    assert body["scope_ids"] == [str(member.id)]
    assert str(foreign.id) not in response.text
    assert str(missing_id) not in response.text
    assert foreign.display_name not in response.text
    assert foreign.email not in response.text
    assert str(member.id) in {item["id"] for item in body["user_access"]}


@pytest.mark.parametrize("requester", ["owner", "admin", "member"])
async def test_custom_permissions_keep_implicit_managers_and_valid_direct_access(permission_directory, requester):
    # Given a custom agent with only a regular member explicitly granted use.
    store, client, agent, owner, member, admin, _foreign = permission_directory
    agent.access_mode = "custom"
    store.actor = {"owner": owner, "admin": admin, "member": member}[requester]
    store.grants.append(
        AgentPermissionRecord(
            id=uuid4(),
            agent_id=agent.id,
            scope_type=PermissionGrantScope.USER,
            scope_id=member.id,
            access_level=PermissionAccessLevel.USE,
        )
    )

    # When a same-tenant implicit manager or direct member reads permissions.
    response = await client.get(f"/api/agents/{agent.id}/permissions")

    # Then implicit creator/admin management and explicit use retain their existing semantics.
    assert response.status_code == 200
    body = response.json()
    expected_level = "use" if requester == "member" else "manage"
    assert body["effective_access_level"] == expected_level
    assert body["can_manage"] is (expected_level == "manage")
    assert body["scope_ids"] == [str(member.id)]
    users = {item["id"]: item for item in body["user_access"]}
    assert users[str(owner.id)]["access_level"] == users[str(admin.id)]["access_level"] == "manage"
    assert users[str(owner.id)]["required_reason"] == "creator"
    assert users[str(admin.id)]["required_reason"] == "company_admin"
    assert users[str(member.id)]["access_level"] == "use"
    assert users[str(member.id)]["is_required"] is False
