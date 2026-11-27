"""Tests for agent permission candidate resolution (DAO path)."""

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter

from app.api import agents as agents_api
from app.dao import identity_dao, user_dao
from app.dao.company_admission_dao import company_admission_dao
from app.records.org import OrgMemberRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.schemas.collaboration_response_agents import PermissionCandidatesResponse
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.sso_service import sso_service


@pytest.mark.asyncio
async def test_get_agent_permission_candidates_resolves_and_lazy_load_safety(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    current_user: Any = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)

    async def fake_check_access(_user, _agent_id):
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

    identity_1 = SimpleNamespace(email="member1@example.com")
    user_1 = SimpleNamespace(
        id=member_1_user_id,
        identity=identity_1,
        tenant_id=tenant_id,
        display_name="Member One",
        email="member1@example.com",
    )

    identity_2 = SimpleNamespace(email="member2@example.com")
    user_2 = SimpleNamespace(
        id=uuid.uuid4(),
        identity=identity_2,
        tenant_id=tenant_id,
        display_name="Member Two",
        email="member2@example.com",
    )

    async def fake_list_permission_candidates(*, tenant_id, search=None, limit=50):
        assert tenant_id == agent.tenant_id
        return [member_1, member_2]

    async def fake_get_many_with_identity(user_ids):
        return {user_1.id: user_1}

    async def fake_linked_platform_user(_org_member, agent_tenant_id=None):
        return user_2

    monkeypatch.setattr(agents_api.org_member_dao, "list_permission_candidates", fake_list_permission_candidates)
    monkeypatch.setattr(agents_api.user_dao, "get_many_with_identity", fake_get_many_with_identity)
    monkeypatch.setattr(
        "app.services.channel_user_service.linked_platform_user",
        fake_linked_platform_user,
    )

    result = await agents_api.get_agent_permission_candidates(
        agent_id=agent_id,
        search=None,
        current_user=current_user,
    )

    assert "users" in result
    result = TypeAdapter(PermissionCandidatesResponse).validate_python(result)
    assert len(result["users"]) == 2
    assert result["users"][0]["name"] == "Member One"
    assert result["users"][0]["username"] == "Member One"
    assert result["users"][1]["name"] == "Member Two"
    assert result["users"][1]["username"] == "Member Two"


def _unlinked_member(tenant_id: uuid.UUID) -> OrgMemberRecord:
    return OrgMemberRecord(id=uuid.uuid4(), tenant_id=tenant_id, name="Unlinked", email="unlinked@example.com")


def _stub_candidates_request(monkeypatch: pytest.MonkeyPatch, tenant_id: uuid.UUID, member: OrgMemberRecord) -> Any:
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    monkeypatch.setattr(agents_api, "check_agent_access", AsyncMock(return_value=(agent, "manage")))
    monkeypatch.setattr(agents_api.org_member_dao, "list_permission_candidates", AsyncMock(return_value=[member]))
    monkeypatch.setattr(agents_api.user_dao, "get_many_with_identity", AsyncMock(return_value={}))
    return agent


async def test_candidates_omit_unlinked_member_and_create_no_user(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an unlinked member that matches no account, in the default end-user organization.
    tenant_id = uuid.uuid4()
    member = _unlinked_member(tenant_id)
    agent = _stub_candidates_request(monkeypatch, tenant_id, member)
    create_identity = AsyncMock()
    create_user = AsyncMock()
    monkeypatch.setattr(sso_service, "match_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(user_dao, "create", create_user)
    monkeypatch.setattr(
        company_admission_dao,
        "tenant",
        AsyncMock(
            return_value=TenantRecord(
                id=tenant_id, name="KABERs", slug="kabers", is_default_end_user_org=True, tier=OrganizationPlanId.PRO
            )
        ),
    )
    current_user: Any = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)

    # When a manager lists the grant candidates.
    result = await agents_api.get_agent_permission_candidates(agent_id=agent.id, search=None, current_user=current_user)

    # Then the member is not a candidate and listing created no identity or user.
    assert result == {"users": [], "agents": []}
    create_identity.assert_not_awaited()
    create_user.assert_not_awaited()


async def test_candidates_omit_member_matching_a_user_of_another_company(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid.uuid4()
    member = _unlinked_member(tenant_id)
    agent = _stub_candidates_request(monkeypatch, tenant_id, member)
    refused = AsyncMock(side_effect=ChannelUserResolutionError("Company admission must be approved first."))
    monkeypatch.setattr("app.services.channel_user_service.linked_platform_user", refused)
    current_user: Any = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)

    result = await agents_api.get_agent_permission_candidates(agent_id=agent.id, search=None, current_user=current_user)

    assert result == {"users": [], "agents": []}
    refused.assert_awaited_once()


async def test_candidates_do_not_hide_a_failed_user_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid.uuid4()
    member = _unlinked_member(tenant_id)
    agent = _stub_candidates_request(monkeypatch, tenant_id, member)
    failed = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr("app.services.channel_user_service.linked_platform_user", failed)
    current_user: Any = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await agents_api.get_agent_permission_candidates(agent_id=agent.id, search=None, current_user=current_user)
