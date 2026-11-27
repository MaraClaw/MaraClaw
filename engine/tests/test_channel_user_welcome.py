import pytest

from app.dao import identity_dao, identity_provider_dao, notification_dao, org_member_dao, tenant_dao
from app.db.session import connection_ctx
from app.records.org import OrgMemberRecord
from app.services import channel_user_service as channel_users
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]

ROW_COUNTS_SQL = (
    "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM identities) AS identities, "
    "(SELECT count(*) FROM org_members) AS members, (SELECT count(*) FROM notifications) AS notifications"
)


@pytest.fixture
async def channel_contact(admission_scenario: AdmissionScenario) -> OrgMemberRecord:
    provider = await identity_provider_dao.create(
        obj_in={"provider_type": "wecom", "name": "Wecom", "tenant_id": admission_scenario.source_id}
    )
    return await org_member_dao.create(
        obj_in={
            "name": "Channel newcomer",
            "email": "newcomer@channel.test",
            "provider_id": provider.id,
            "tenant_id": admission_scenario.source_id,
            "external_id": "channel-newcomer",
        }
    )


@pytest.mark.parametrize("system_org", [False, True])
async def test_unmatched_channel_contact_has_no_platform_user(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, system_org: bool
) -> None:
    # Given a contact that is neither linked nor matched, in an organization that once let channels create accounts.
    scenario = admission_scenario
    if system_org:
        tenant = await tenant_dao.get(scenario.source_id)
        assert tenant is not None
        await tenant_dao.update(db_obj=tenant, obj_in={"is_default_end_user_org": False, "is_system": True})
    before = await scenario.db.fetchone(ROW_COUNTS_SQL)
    # When the connector resolves the contact without a caller-owned transaction.
    user = await channel_users.linked_platform_user(channel_contact, scenario.source_id)
    # Then the contact has no platform user and nothing was created: no identity, user, link, membership or welcome.
    assert user is None
    assert await scenario.db.fetchone(ROW_COUNTS_SQL) == before
    contact = await org_member_dao.get(channel_contact.id)
    assert contact is not None
    assert contact.user_id is None
    assert await identity_dao.get_by_email(channel_contact.email or "") is None


async def test_linked_channel_account_receives_no_welcome(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord
) -> None:
    # Given a contact linked to an existing account of its organization.
    scenario = admission_scenario
    linked = await org_member_dao.update(db_obj=channel_contact, obj_in={"user_id": scenario.applicant.id})
    # When a connector event resolves its linked contact.
    user = await channel_users.linked_platform_user(linked, scenario.source_id)
    # Then resolution reuses the account without an admission welcome.
    assert user is not None
    assert user.id == scenario.applicant.id
    assert await notification_dao.list_for_user(user.id) == []


@pytest.mark.parametrize("by_phone", [False, True])
async def test_matched_channel_account_receives_no_welcome(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord, by_phone: bool
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
    user = await channel_users.linked_platform_user(contact, scenario.source_id)
    # Then it links the existing account without treating this as account onboarding.
    assert user is not None
    assert user.id == scenario.applicant.id
    stored = await org_member_dao.get(contact.id)
    assert stored is not None
    assert stored.user_id == scenario.applicant.id
    assert await scenario.db.fetchall("SELECT id FROM notifications") == []


async def test_unmatched_channel_contact_without_assigned_tenant_has_no_platform_user(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord
) -> None:
    # Given an unmatched contact and no tenant on the agent.
    before = await admission_scenario.db.fetchone(ROW_COUNTS_SQL)
    # When the connector resolves it.
    user = await channel_users.linked_platform_user(channel_contact, None)
    # Then it has no platform user and no account was invented for the contact's organization.
    assert user is None
    assert await admission_scenario.db.fetchone(ROW_COUNTS_SQL) == before


async def test_channel_link_joins_caller_transaction(
    admission_scenario: AdmissionScenario, channel_contact: OrgMemberRecord
) -> None:
    # Given a contact matching an existing account, and a caller-owned transaction that fails after the link.
    scenario = admission_scenario
    identity = scenario.applicant.identity
    assert identity is not None
    contact = await org_member_dao.update(db_obj=channel_contact, obj_in={"email": identity.email})

    async def caller_transaction() -> None:
        async with connection_ctx():
            user = await channel_users.linked_platform_user(contact, scenario.source_id)
            assert user is not None
            assert user.id == scenario.applicant.id
            raise RuntimeError("caller failed")

    # When the link joins that transaction rather than committing independently.
    with pytest.raises(RuntimeError, match="caller failed"):
        await caller_transaction()
    # Then the link does not escape the caller's rollback.
    stored = await org_member_dao.get(contact.id)
    assert stored is not None
    assert stored.user_id is None
