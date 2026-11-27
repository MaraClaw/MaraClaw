from uuid import uuid4

import pytest

from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.org_member_dao import org_member_dao
from app.records.agent import AgentRecord
from app.services.channel_user_service import ChannelUserResolutionError, linked_platform_user
from app.services.channels.binding import resolve_bound_sender
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]

ROW_COUNTS_SQL = (
    "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM identities) AS identities, "
    "(SELECT count(*) FROM identity_providers) AS providers, (SELECT count(*) FROM org_members) AS members"
)


async def test_channel_sender_cannot_create_company_membership(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    agent = AgentRecord(id=uuid4(), creator_id=scenario.genesis.id, name="Company Agent", tenant_id=scenario.target_id)
    before = await scenario.db.fetchone(ROW_COUNTS_SQL)
    with pytest.raises(ChannelUserResolutionError):
        await resolve_bound_sender(
            agent=agent,
            channel_type="slack",
            external_user_id="new-channel-user",
            extra_info={"name": "Channel Applicant"},
        )
    assert (
        await scenario.db.fetchval("SELECT count(*) FROM users WHERE tenant_id=%(id)s", {"id": scenario.target_id}) == 2
    )
    assert await scenario.db.fetchone(ROW_COUNTS_SQL) == before


async def test_unbound_org_member_has_no_platform_user_and_creates_none(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    provider = await identity_provider_dao.create(
        obj_in={"provider_type": "slack", "name": "Slack", "tenant_id": scenario.target_id}
    )
    member = await org_member_dao.create(
        obj_in={
            "provider_id": provider.id,
            "tenant_id": scenario.target_id,
            "name": "Unadmitted",
            "external_id": "unbound",
        }
    )
    before = await scenario.db.fetchone(ROW_COUNTS_SQL)
    assert await linked_platform_user(member, scenario.target_id) is None
    assert await scenario.db.fetchone(ROW_COUNTS_SQL) == before
    stored = await org_member_dao.get(member.id)
    assert stored is not None
    assert stored.user_id is None


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("inbound", [False, True])
async def test_channel_cannot_link_source_member_as_company_member(
    admission_scenario: AdmissionScenario, inbound: bool, linked: bool
):
    # Given a company contact whose email, and optionally whose link, belongs to a member of another organization.
    scenario = admission_scenario
    agent = AgentRecord(id=uuid4(), creator_id=scenario.genesis.id, name="Company Agent", tenant_id=scenario.target_id)
    provider = await identity_provider_dao.create(
        obj_in={"provider_type": "slack", "name": "Slack", "tenant_id": scenario.target_id}
    )
    member = await org_member_dao.create(
        obj_in={
            "provider_id": provider.id,
            "tenant_id": scenario.target_id,
            "name": "Source member",
            "external_id": "source-member",
            "email": "applicant@acme.test",
            "user_id": scenario.applicant.id if linked else None,
        }
    )
    # When an inbound sender or an outbound tool resolves that contact to a platform user.
    attempt = (
        resolve_bound_sender(
            agent=agent,
            channel_type="slack",
            external_user_id="source-member",
            extra_info={"email": "applicant@acme.test"},
        )
        if inbound
        else linked_platform_user(member, scenario.target_id)
    )
    with pytest.raises(ChannelUserResolutionError):
        await attempt
    # Then the source member stays in the source organization and the contact is not re-linked.
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    stored = await org_member_dao.get(member.id)
    assert stored is not None
    assert stored.user_id == (scenario.applicant.id if linked else None)
