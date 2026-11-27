import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.records.org import OrgMemberRecord
from app.services.channel_user_service import ChannelUserService, linked_platform_user
from app.services.sso_service import sso_service


def test_sso_identity_lookup_chain_prioritizes_unionid_then_userid_then_openid():
    lookup_chain = sso_service._identity_lookup_chain(
        "feishu",
        "ou_open_123",
        {
            "raw_data": {
                "open_id": "ou_open_123",
                "union_id": "on_union_456",
                "user_id": "u_emp_789",
            }
        },
    )

    assert lookup_chain == [
        ("unionid", "on_union_456"),
        ("external_id", "u_emp_789"),
        ("open_id", "ou_open_123"),
    ]


def test_sso_extract_identity_ids_uses_real_union_id_not_open_id():
    union_id, open_id, external_id = sso_service._extract_identity_ids(
        "feishu",
        "ou_open_123",
        {
            "raw_data": {
                "open_id": "ou_open_123",
                "union_id": "on_union_456",
                "user_id": "u_emp_789",
            }
        },
    )

    assert union_id == "on_union_456"
    assert open_id == "ou_open_123"
    assert external_id == "u_emp_789"


def test_sso_extract_identity_ids_handles_registration_wrapped_payload():
    union_id, open_id, external_id = sso_service._extract_identity_ids(
        "dingtalk",
        "open_123",
        {
            "name": "Alice",
            "raw_data": {
                "openId": "open_123",
                "unionId": "union_456",
            },
        },
    )

    assert union_id == "union_456"
    assert open_id == "open_123"
    assert external_id is None


def test_channel_user_service_keeps_feishu_user_id_out_of_unionid():
    service = ChannelUserService()

    union_id, open_id, external_id = service._get_channel_ids(
        "feishu",
        "ou_open_123",
        {
            "external_id": "u_emp_789",
            "unionid": "on_union_456",
            "open_id": "ou_open_123",
        },
    )

    assert union_id == "on_union_456"
    assert open_id == "ou_open_123"
    assert external_id == "u_emp_789"


def test_channel_user_service_maps_generic_channels_to_dedicated_provider():
    service = ChannelUserService()

    assert service._normalize_channel_type("wechat") == "wechat"
    assert service._normalize_channel_type("slack") == "slack"
    assert service._normalize_channel_type("teams") == "teams"
    assert service._normalize_channel_type("microsoft_teams") == "teams"
    assert service._normalize_channel_type("feishu") == "feishu"


def test_channel_user_service_keeps_generic_channel_external_ids_unscoped():
    service = ChannelUserService()

    assert service._get_channel_ids("wechat", "wx_user_123", {}) == (None, None, "wx_user_123")
    assert service._get_channel_ids("slack", "U123456", {}) == (None, None, "U123456")
    assert service._get_channel_ids("teams", "29:abc", {}) == (None, None, "29:abc")


@pytest.mark.asyncio
async def test_channel_user_service_uses_feishu_open_id_for_existing_member_lookup(monkeypatch):
    service = ChannelUserService()
    expected_member = SimpleNamespace(id=uuid.uuid4(), name="Feishu member")
    provider_id = uuid.uuid4()
    lookup = AsyncMock(return_value=expected_member)
    monkeypatch.setattr("app.services.channel_user_service.org_member_dao.find_active_by_any_ids", lookup)

    member = await service._find_org_member(
        provider_id=provider_id,
        channel_type="feishu",
        external_user_id=None,
        extra_info={"open_id": "ou_open_123"},
    )

    assert member is expected_member
    lookup.assert_awaited_once()
    assert lookup.await_args is not None
    kwargs = lookup.await_args.kwargs
    assert kwargs["provider_id"] == provider_id
    assert kwargs["open_id"] == "ou_open_123"


@pytest.mark.asyncio
async def test_channel_user_service_skips_dingtalk_lookup_when_ids_missing(monkeypatch):
    service = ChannelUserService()
    session = MagicMock()
    monkeypatch.setattr("app.services.channel_user_service.org_member_dao.session", session)
    provider_id = uuid.uuid4()

    member = await service._find_org_member(
        provider_id=provider_id,
        channel_type="dingtalk",
        external_user_id=None,
        extra_info={},
    )

    assert member is None
    session.assert_not_called()


@pytest.mark.asyncio
async def test_channel_user_service_uses_wechat_external_id_for_existing_member_lookup(monkeypatch):
    service = ChannelUserService()
    expected_member = SimpleNamespace(id=uuid.uuid4(), name="WeChat member")
    provider_id = uuid.uuid4()
    lookup = AsyncMock(return_value=expected_member)
    monkeypatch.setattr("app.services.channel_user_service.org_member_dao.find_active_by_any_ids", lookup)

    member = await service._find_org_member(
        provider_id=provider_id,
        channel_type="wechat",
        external_user_id="wx_user_123",
        extra_info={"external_id": "wx_user_123"},
    )

    assert member is expected_member
    lookup.assert_awaited_once()
    assert lookup.await_args is not None
    kwargs = lookup.await_args.kwargs
    assert kwargs["provider_id"] == provider_id
    assert kwargs["external_id"] == "wx_user_123"


@pytest.mark.asyncio
async def test_linked_platform_user_serializes_tenant_id_for_sso_matches(monkeypatch):
    tenant_id = uuid.uuid4()
    matched_user = SimpleNamespace(
        id=uuid.uuid4(),
        identity=SimpleNamespace(id=uuid.uuid4(), email="alice@example.com"),
        tenant_id=tenant_id,
        display_name="Alice",
        role="member",
    )
    member = OrgMemberRecord(
        id=uuid.uuid4(), name="Alice", email="alice@example.com", phone="15555550123", tenant_id=tenant_id
    )
    email_match = AsyncMock(return_value=None)
    mobile_match = AsyncMock(return_value=matched_user)
    monkeypatch.setattr(sso_service, "match_user_by_email", email_match)
    monkeypatch.setattr(sso_service, "match_user_by_mobile", mobile_match)
    monkeypatch.setattr("app.services.channel_user_service.org_member_dao.update", AsyncMock())
    monkeypatch.setattr(
        "app.services.channel_user_service.user_dao.get_with_identity", AsyncMock(return_value=matched_user)
    )

    user = await linked_platform_user(member, tenant_id)

    assert user is matched_user
    email_match.assert_awaited_once_with("alice@example.com", str(tenant_id))
    mobile_match.assert_awaited_once_with("15555550123", str(tenant_id))
