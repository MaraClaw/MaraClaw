import uuid
from unittest.mock import AsyncMock

import pytest

from app.dao import org_member_dao
from app.records.agent import AgentRecord
from app.records.identity import IdentityProviderRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import binding


@pytest.mark.parametrize(
    ("channel", "external", "extra", "lookup"),
    [
        ("feishu", None, {"open_id": "ou_open"}, {"open_id": "ou_open", "unionid": None, "external_id": None}),
        (
            "feishu",
            "tenant_user",
            {"open_id": "ou_open", "external_id": "tenant_user"},
            {"open_id": "ou_open", "unionid": None, "external_id": "tenant_user"},
        ),
        ("dingtalk", "staff", {"unionid": "union"}, {"external_id": "staff", "unionid": "union"}),
        ("wecom", "staff", {"unionid": "other"}, {"external_id": "staff"}),
    ],
)
async def test_direct_identity_lookup_preserves_provider_namespaces(monkeypatch, channel, external, extra, lookup):
    # Given
    tenant_id = uuid.uuid4()
    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), tenant_id=tenant_id, name="Agent")
    user = UserRecord(id=uuid.uuid4(), tenant_id=tenant_id)
    provider = IdentityProviderRecord(id=uuid.uuid4(), provider_type=channel, name=channel, tenant_id=tenant_id)
    member = OrgMemberRecord(id=uuid.uuid4(), provider_id=provider.id, user_id=user.id, name="Human")
    find_member = AsyncMock(return_value=member)
    monkeypatch.setattr(binding.identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=provider))
    monkeypatch.setattr(org_member_dao, "find_active_by_any_ids", find_member)
    monkeypatch.setattr(binding.user_dao, "get_with_identity", AsyncMock(return_value=user))
    # When
    resolved = await binding.resolve_bound_sender(
        agent=agent, channel_type=channel, external_user_id=external, extra_info=extra
    )
    # Then
    assert resolved.id == user.id
    find_member.assert_awaited_once_with(provider_id=provider.id, **lookup)


@pytest.mark.parametrize("rejection", ["provider_inactive", "member_inactive", "other_tenant"])
async def test_direct_identity_lookup_rejects_inactive_or_foreign_binding(monkeypatch, rejection):
    # Given
    tenant_id = uuid.uuid4()
    agent = AgentRecord(id=uuid.uuid4(), creator_id=uuid.uuid4(), tenant_id=tenant_id, name="Agent")
    user = UserRecord(id=uuid.uuid4(), tenant_id=uuid.uuid4() if rejection == "other_tenant" else tenant_id)
    provider = IdentityProviderRecord(
        id=uuid.uuid4(), provider_type="slack", name="Slack", is_active=rejection != "provider_inactive"
    )
    member = OrgMemberRecord(
        id=uuid.uuid4(),
        provider_id=provider.id,
        user_id=user.id,
        name="Human",
        status="inactive" if rejection == "member_inactive" else "active",
    )
    monkeypatch.setattr(binding.identity_provider_dao, "get_by_type_and_tenant", AsyncMock(return_value=provider))
    monkeypatch.setattr(org_member_dao, "find_active_by_any_ids", AsyncMock(return_value=member))
    monkeypatch.setattr(binding.user_dao, "get_with_identity", AsyncMock(return_value=user))
    # When / Then
    with pytest.raises(ChannelUserResolutionError):
        await binding.resolve_bound_sender(agent=agent, channel_type="slack", external_user_id="U0", extra_info={})
