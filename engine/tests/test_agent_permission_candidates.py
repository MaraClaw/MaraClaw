"""Tests for agent permission candidate resolution (DAO path)."""

import uuid
from types import SimpleNamespace

import pytest

from app.api import agents as agents_api
from app.records.org import OrgMemberRecord


@pytest.mark.asyncio
async def test_get_agent_permission_candidates_resolves_and_lazy_load_safety(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    current_user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_check_access(_user, _agent_id, _db=None):
        return agent, "manage"

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_access)

    member_1_user_id = uuid.uuid4()
    member_1 = OrgMemberRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Member One",
        status="active",
        user_id=member_1_user_id,
        email="member1@example.com",
    )
    member_2 = OrgMemberRecord(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Member Two",
        status="active",
        user_id=None,
        email="member2@example.com",
    )

    identity_1 = SimpleNamespace(username="member_one", email="member1@example.com")
    user_1 = SimpleNamespace(
        id=member_1_user_id,
        identity=identity_1,
        tenant_id=tenant_id,
        username="member_one",
        email="member1@example.com",
    )

    identity_2 = SimpleNamespace(username="member_two", email="member2@example.com")
    user_2 = SimpleNamespace(
        id=uuid.uuid4(), identity=identity_2, tenant_id=tenant_id, username="member_two", email="member2@example.com"
    )

    async def fake_list_permission_candidates(*, tenant_id, search=None, limit=50):
        assert tenant_id == agent.tenant_id
        return [member_1, member_2]

    async def fake_get_many_with_identity(user_ids):
        return {user_1.id: user_1}

    async def fake_resolve_or_create(_org_member, agent_tenant_id=None, db=None):
        return user_2

    monkeypatch.setattr(agents_api.org_member_dao, "list_permission_candidates", fake_list_permission_candidates)
    monkeypatch.setattr(agents_api.user_dao, "get_many_with_identity", fake_get_many_with_identity)
    monkeypatch.setattr(
        "app.services.channel_user_service.get_platform_user_by_org_member",
        fake_resolve_or_create,
    )

    result = await agents_api.get_agent_permission_candidates(
        agent_id=agent_id,
        search=None,
        current_user=current_user,
    )

    assert len(result["users"]) == 2
    assert result["users"][0]["name"] == "Member One"
    assert result["users"][0]["username"] == "member_one"
    assert result["users"][1]["name"] == "Member Two"
    assert result["users"][1]["username"] == "member_two"
