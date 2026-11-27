from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.dao import identity_dao
from app.dao.company_admission_dao import company_admission_dao
from app.records.agent import AgentRecord
from app.records.identity import IdentityRecord
from app.records.org import OrgMemberRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import channel_user_service as channel_users
from app.services.agent_tool_exec import okr_write_objectives


@pytest.mark.parametrize(
    ("email", "phone"),
    [(None, None), ("ada@example.com", None), (None, "15555550123"), ("ada@example.com", "15555550123")],
)
async def test_unmatched_channel_contact_creates_no_identity_or_user(monkeypatch, email: str | None, phone: str | None):
    # Given a contact in the default end-user organization that is unlinked and matches no account.
    tenant_id = uuid4()
    identity = IdentityRecord(id=uuid4(), email=email)
    user = UserRecord(id=uuid4(), identity_id=identity.id, tenant_id=tenant_id, identity=identity)
    member = OrgMemberRecord(id=uuid4(), name="Ada", email=email, phone=phone, tenant_id=tenant_id)
    create_identity = AsyncMock(return_value=identity)
    create_user = AsyncMock(return_value=user)
    link = AsyncMock()
    monkeypatch.setattr(channel_users.sso_service, "match_user_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_users.sso_service, "match_user_by_mobile", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_users.org_member_dao, "update", link)
    monkeypatch.setattr(identity_dao, "get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(identity_dao, "get_by_phone", AsyncMock(return_value=None))
    monkeypatch.setattr(identity_dao, "create_identity", create_identity)
    monkeypatch.setattr(channel_users.user_dao, "create", create_user)
    monkeypatch.setattr(channel_users.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(
        company_admission_dao,
        "tenant",
        AsyncMock(
            return_value=TenantRecord(
                id=tenant_id, name="KABERs", slug="kabers", is_default_end_user_org=True, tier=OrganizationPlanId.PRO
            )
        ),
    )

    # When an outbound tool resolves it.
    result = await channel_users.linked_platform_user(member, tenant_id)

    # Then it has no platform user and no identity, user or link was written.
    assert result is None
    create_identity.assert_not_awaited()
    create_user.assert_not_awaited()
    link.assert_not_awaited()


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
    monkeypatch.setattr(okr_write_objectives.user_dao, "get", AsyncMock(return_value=owner))
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
