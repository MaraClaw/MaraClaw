from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao.company_admission_dao import company_admission_dao
from app.records.agent import AgentRecord
from app.records.identity import IdentityRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import channel_user_service as channel_users
from app.services.agent_tool_exec import okr_write_objectives


@pytest.mark.parametrize(
    ("profile", "expected_name", "expected_email"),
    [
        ({}, "User", None),
        ({"name": "Ada"}, "Ada", None),
        ({"email": "ada@example.com"}, "ada@example.com", "ada@example.com"),
        ({"name": "Ada", "email": "ada@example.com"}, "Ada", "ada@example.com"),
    ],
)
async def test_channel_provisioning_uses_real_contact_without_manufactured_handles(
    monkeypatch, profile: dict[str, str], expected_name: str, expected_email: str | None
):
    tenant_id = uuid4()
    identity = IdentityRecord(id=uuid4(), email=expected_email)
    user = UserRecord(id=uuid4(), identity_id=identity.id, tenant_id=tenant_id, identity=identity)
    lookup = AsyncMock(return_value=None)
    create_identity = AsyncMock(return_value=identity)
    create_user = AsyncMock(return_value=user)
    monkeypatch.setattr(channel_users.identity_dao, "get_by_email", lookup)
    monkeypatch.setattr(channel_users.identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(channel_users.user_dao, "create", create_user)
    monkeypatch.setattr(channel_users.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(
        company_admission_dao,
        "tenant",
        AsyncMock(
            return_value=TenantRecord(id=tenant_id, name="KABERs", slug="kabers", is_default_end_user_org=True)
        ),
    )

    result = await channel_users.ChannelUserService()._create_channel_user("feishu", "external-123", profile, tenant_id)

    assert result is user
    create_identity.assert_awaited_once_with(
        email=expected_email, phone=None, password_hash=None, is_platform_admin=False, email_verified=True
    )
    assert create_user.await_args is not None
    fields = create_user.await_args.kwargs["obj_in"]
    assert fields["identity_id"] == identity.id
    assert fields["tenant_id"] == tenant_id
    assert fields["display_name"] == expected_name
    if expected_email is None:
        lookup.assert_not_awaited()
    else:
        lookup.assert_awaited_once_with(expected_email)


async def test_okr_owner_email_resolves_to_membership_in_agent_tenant(monkeypatch):
    tenant_id, user_id = uuid4(), uuid4()
    agent = AgentRecord(id=uuid4(), name="OKR", creator_id=user_id, tenant_id=tenant_id)
    owner = UserRecord(id=user_id, tenant_id=tenant_id)
    lookup = AsyncMock(return_value=owner)
    create = AsyncMock(return_value=SimpleNamespace(id=uuid4(), title="Ship"))
    access = SimpleNamespace(
        _load_okr_request_context=AsyncMock(return_value={"agent": agent, "agent_is_system": True}),
        _can_create_okr_target=lambda *_args: None,
    )
    monkeypatch.setattr(okr_write_objectives, "_okr_access_module", lambda: access)
    monkeypatch.setattr(okr_write_objectives.user_dao, "find_by_email_or_display_name", lookup)
    monkeypatch.setattr(okr_write_objectives.okr_objective_dao, "create", create)

    result = await okr_write_objectives._create_objective(
        agent.id,
        user_id,
        {
            "title": "Ship",
            "owner_type": "user",
            "owner_name": "ada@example.com",
            "period_start": "2026-10-01",
            "period_end": "2026-12-31",
        },
    )

    lookup.assert_awaited_once_with("ada@example.com", tenant_id=tenant_id)
    assert create.await_args is not None
    fields = create.await_args.kwargs["obj_in"]
    assert fields["owner_id"] == user_id
    assert fields["tenant_id"] == tenant_id
    assert fields["period_start"] == date(2026, 10, 1)
    assert result.startswith("Successfully created Objective")
