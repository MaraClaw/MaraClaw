from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.dao import identity_dao, notification_dao, org_member_dao, tenant_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.org import OrgMemberRecord
from app.records.user import UserRecord
from app.services import channel_user_service as channel_users
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


@pytest.fixture
async def channel_contact(admission_scenario: AdmissionScenario) -> OrgMemberRecord:
    provider = await channel_users.channel_user_service._ensure_provider("wecom", admission_scenario.source_id)
    return await org_member_dao.create(
        obj_in={
            "name": "Channel newcomer",
            "email": "newcomer@channel.test",
            "provider_id": provider.id,
            "tenant_id": admission_scenario.source_id,
            "external_id": "channel-newcomer",
        }
    )


async def _resolve(contact: OrgMemberRecord, tenant_id: UUID | None, via_member: bool) -> UserRecord:
    if via_member:
        return await channel_users.get_platform_user_by_org_member(contact, tenant_id)
    agent = AgentRecord(id=uuid4(), name="Channel agent", creator_id=uuid4(), tenant_id=tenant_id)
    return await channel_users.channel_user_service.resolve_channel_user(
        agent,
        "wecom",
        contact.external_id,
        {"name": contact.name, "email": contact.email, "mobile": contact.phone},
    )


@pytest.mark.parametrize("via_member", [False, True])
@pytest.mark.parametrize("system_org", [False, True])
async def test_created_channel_account_receives_no_welcome(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, via_member: bool, system_org: bool
) -> None:
    # Given a new contact in an organization where channel account creation is permitted.
    scenario = admission_scenario
    if system_org:
        tenant = await tenant_dao.get(scenario.source_id)
        assert tenant is not None
        await tenant_dao.update(db_obj=tenant, obj_in={"is_default_end_user_org": False, "is_system": True})
    # When the connector creates the platform account without a caller-owned transaction.
    user = await _resolve(channel_contact, scenario.source_id, via_member)
    # Then the account is linked and assigned without emitting an admission welcome.
    assert user.tenant_id == scenario.source_id
    assert user.email == channel_contact.email
    linked = await org_member_dao.get(channel_contact.id)
    assert linked is not None
    assert linked.user_id == user.id
    assert await scenario.db.fetchall("SELECT id FROM notifications") == []


@pytest.mark.parametrize("via_member", [False, True])
async def test_linked_channel_account_receives_no_welcome(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, via_member: bool
) -> None:
    # Given a channel-created account with no admission decision.
    scenario = admission_scenario
    existing = await _resolve(channel_contact, scenario.source_id, via_member)
    linked = await org_member_dao.get(channel_contact.id)
    assert linked is not None
    # When a subsequent connector event resolves its linked contact.
    user = await _resolve(linked, scenario.source_id, via_member)
    # Then resolution reuses the account without an admission welcome.
    assert user.id == existing.id
    assert await notification_dao.list_for_user(user.id) == []


@pytest.mark.parametrize("via_member", [False, True])
@pytest.mark.parametrize("by_phone", [False, True])
async def test_matched_channel_account_receives_no_welcome(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, via_member: bool, by_phone: bool
) -> None:
    # Given an existing platform account matched by email or phone, but not yet linked.
    scenario = admission_scenario
    identity = scenario.applicant.identity
    assert identity is not None
    await identity_dao.update(db_obj=identity, obj_in={"phone": "15551234567"})
    contact = await org_member_dao.update(
        db_obj=channel_contact,
        obj_in={"email": None if by_phone else identity.email, "phone": "+15551234567" if by_phone else None},
    )
    # When the connector resolves the contact.
    user = await _resolve(contact, scenario.source_id, via_member)
    # Then it links the existing account without treating this as account onboarding.
    assert user.id == scenario.applicant.id
    assert await scenario.db.fetchall("SELECT id FROM notifications") == []


@pytest.mark.parametrize("via_member", [False, True])
async def test_channel_account_without_assigned_tenant_receives_no_welcome(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, via_member: bool
) -> None:
    # Given a contact whose organization must not replace an absent agent tenant.
    # When the connector creates an account without an assigned tenant.
    user = await _resolve(channel_contact, None, via_member)
    # Then it preserves that assignment behavior and does not invent an organization welcome.
    assert user.tenant_id is None
    assert await admission_scenario.db.fetchall("SELECT id FROM notifications") == []


@pytest.mark.parametrize("via_member", [False, True])
async def test_channel_creation_does_not_load_tenant_for_welcome(
    admission_scenario: AdmissionScenario,
    channel_contact: OrgMemberRecord,
    monkeypatch: pytest.MonkeyPatch,
    via_member: bool,
) -> None:
    # Given a concrete assignment that requires no welcome-related tenant lookup.
    load_tenant = AsyncMock(side_effect=RuntimeError("unexpected welcome tenant lookup"))
    monkeypatch.setattr(tenant_dao, "get", load_tenant)
    # When the connector creates the account.
    user = await _resolve(channel_contact, admission_scenario.source_id, via_member)
    # Then account creation succeeds without loading the tenant or sending a welcome.
    assert user.tenant_id == admission_scenario.source_id
    load_tenant.assert_not_awaited()
    assert await admission_scenario.db.fetchall("SELECT id FROM notifications") == []


@pytest.mark.parametrize("via_member", [False, True])
async def test_channel_creation_does_not_depend_on_welcome_delivery(
    admission_scenario: AdmissionScenario,
    channel_contact: OrgMemberRecord,
    monkeypatch: pytest.MonkeyPatch,
    via_member: bool,
) -> None:
    # Given an unavailable welcome helper, which channel creation must not invoke.
    scenario = admission_scenario
    welcome = AsyncMock(side_effect=RuntimeError("welcome delivery failed"))
    monkeypatch.setattr("app.services.org_welcome.send_organization_welcome", welcome)
    # When the connector creates the account.
    user = await _resolve(channel_contact, scenario.source_id, via_member)
    # Then creation and linking succeed independently, with no helper call or notification.
    assert user.tenant_id == scenario.source_id
    assert user.email == channel_contact.email
    welcome.assert_not_awaited()
    assert await scenario.db.fetchall("SELECT id FROM notifications") == []
    contact = await org_member_dao.get(channel_contact.id)
    assert contact is not None
    assert contact.user_id == user.id


@pytest.mark.parametrize("via_member", [False, True])
async def test_channel_creation_joins_caller_transaction(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, via_member: bool
) -> None:
    # Given a caller-owned transaction that fails after account creation.
    scenario = admission_scenario

    async def caller_transaction() -> None:
        async with connection_ctx():
            await _resolve(channel_contact, scenario.source_id, via_member)
            raise RuntimeError("caller failed")

    # When the channel creation joins it rather than committing independently.
    with pytest.raises(RuntimeError, match="caller failed"):
        await caller_transaction()
    # Then the account does not escape the caller's rollback, and no welcome was emitted.
    assert await identity_dao.get_by_email(channel_contact.email or "") is None
    assert await scenario.db.fetchval("SELECT COUNT(*) FROM users") == 4
    assert await scenario.db.fetchall("SELECT id FROM notifications") == []
