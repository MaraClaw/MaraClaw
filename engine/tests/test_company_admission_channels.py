from uuid import uuid4

import pytest

from app.dao.identity_provider_dao import identity_provider_dao
from app.dao.org_member_dao import org_member_dao
from app.records.agent import AgentRecord
from app.services.channel_user_service import (
    ChannelUserResolutionError,
    channel_user_service,
    get_platform_user_by_org_member,
)
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


async def test_channel_sender_cannot_create_company_membership(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    agent = AgentRecord(id=uuid4(), creator_id=scenario.genesis.id, name="Company Agent", tenant_id=scenario.target_id)
    with pytest.raises(ChannelUserResolutionError):
        await channel_user_service.resolve_channel_user(
            agent, "slack", "new-channel-user", {"name": "Channel Applicant"}
        )
    assert (
        await scenario.db.fetchval("SELECT count(*) FROM users WHERE tenant_id=%(id)s", {"id": scenario.target_id}) == 2
    )


async def test_unbound_org_member_cannot_create_company_membership(admission_scenario: AdmissionScenario):
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
    with pytest.raises(ChannelUserResolutionError):
        await get_platform_user_by_org_member(member, scenario.target_id)
    assert (
        await scenario.db.fetchval("SELECT count(*) FROM users WHERE tenant_id=%(id)s", {"id": scenario.target_id}) == 2
    )


async def test_channel_cannot_link_source_member_as_company_member(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    agent = AgentRecord(id=uuid4(), creator_id=scenario.genesis.id, name="Company Agent", tenant_id=scenario.target_id)
    with pytest.raises(ChannelUserResolutionError):
        await channel_user_service.resolve_channel_user(
            agent, "slack", "source-member", {"email": "applicant@acme.test"}
        )
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert (
        await scenario.db.fetchval(
            "SELECT count(*) FROM org_members WHERE user_id=%(id)s AND tenant_id=%(target)s",
            {"id": scenario.applicant.id, "target": scenario.target_id},
        )
        == 0
    )
