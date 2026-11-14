"""Paid company transfer into a tier-governed organization, through the real review stack and PostgreSQL."""

from pathlib import Path
from uuid import UUID

import pytest

import app.dao.whatsapp_personal_dao as whatsapp_dao
from app.dao.agent_dao import agent_dao
from app.dao.user_dao import user_dao
from app.records.company_transfer import TransferChoice
from app.records.organization_plan import OrganizationPlanId
from app.services import (
    company_transfer_cleanup,
    company_transfer_fence,
    company_transfer_guards as guards,
    company_transfer_requests as requests,
)
from app.services.company_capacity import CompanyAccountLimitReachedError, company_usage
from capacity_fixtures import grant_extras
from company_admission_fixtures import AdmissionScenario, admission_scenario
from invitation_fixtures import pending_domain_transfer
from test_company_transfer_postgres import submit_paid, transfer_scenario
from tier_limit_fixtures import approve_admission_for

__all__ = ["admission_scenario", "transfer_scenario"]


@pytest.fixture
def revocations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[UUID]:
    """Spy on WhatsApp revocation (the first disposition write) and disable runtime cleanup."""
    seen: list[UUID] = []
    original = whatsapp_dao.revoke

    async def spy(agent_id: UUID, *, purge: bool) -> object:
        seen.append(agent_id)
        return await original(agent_id, purge=purge)

    async def no_runtime() -> None:
        return None

    monkeypatch.setattr(whatsapp_dao, "revoke", spy)
    monkeypatch.setattr(company_transfer_fence.agent_manager, "_agent_dir", lambda _id: tmp_path / str(_id))
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", no_runtime)
    monkeypatch.setattr(guards, "get_transfer_policy", requests.get_transfer_policy)
    return seen


async def _seed(scenario: AdmissionScenario, name: str, *, tenant: UUID | None = None, **values: object) -> UUID:
    agent = await agent_dao.create(
        obj_in={
            "name": name,
            "creator_id": scenario.applicant.id,
            "tenant_id": tenant or scenario.source_id,
            "status": "running",
            **values,
        }
    )
    return agent.id


async def _approve(scenario: AdmissionScenario, choice: TransferChoice):
    if choice is TransferChoice.PAID:
        request_id = await submit_paid(scenario)
        confirmed = await scenario.client.post(
            f"/api/company-admissions/transfers/{request_id}/decision",
            json={"decision": "approved"},
            headers=scenario.headers(scenario.outsider),
        )
        assert confirmed.status_code == 200, confirmed.text
        admission_id = confirmed.json()["admission_request_id"]
    else:
        admission_id = (await pending_domain_transfer(scenario, choice)).id
    return await scenario.client.post(
        f"/api/company-admissions/{admission_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )


async def _tenant_of(scenario: AdmissionScenario, agent_id: UUID) -> UUID:
    tenant = await scenario.db.fetchval("SELECT tenant_id FROM agents WHERE id=%(id)s", {"id": agent_id})
    assert isinstance(tenant, UUID)
    return tenant


async def test_exact_fit_of_ordinary_incoming_agents_migrates_everything(
    transfer_scenario: AdmissionScenario, revocations: list[UUID]
):
    scenario = transfer_scenario
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)
    ordinary = [await _seed(scenario, f"Ordinary {n}") for n in range(2)]
    extras = [
        await _seed(scenario, "System", is_system=True),
        await _seed(scenario, "Expired", is_expired=True),
    ]
    response = await _approve(scenario, TransferChoice.PAID)
    assert response.status_code == 200, response.text
    assert {await _tenant_of(scenario, agent_id) for agent_id in ordinary + extras} == {scenario.target_id}
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    assert set(revocations) == set(ordinary + extras)


@pytest.mark.parametrize("existing_in_destination", [0, 1])
async def test_one_agent_over_the_cap_refuses_the_whole_batch_before_any_write(
    transfer_scenario: AdmissionScenario, revocations: list[UUID], existing_in_destination: int
):
    scenario = transfer_scenario
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)
    for index in range(existing_in_destination):
        _ = await _seed(scenario, f"Already there {index}", tenant=scenario.target_id)
    incoming = [await _seed(scenario, f"Ordinary {n}") for n in range(3 - existing_in_destination)]
    response = await _approve(scenario, TransferChoice.PAID)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "transfer_destination_quota_exceeded"
    assert {await _tenant_of(scenario, agent_id) for agent_id in incoming} == {scenario.source_id}
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert revocations == []
    status = await scenario.db.fetchone("SELECT status FROM company_admission_requests")
    assert status == {"status": "pending"}


async def _fill_destination_with_colleagues_agents(scenario: AdmissionScenario) -> UUID:
    """Two agents owned by the destination's administrator (the applicant's own count there stays 0), then one incoming."""
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)  # company cap: 2 agents
    for index in range(2):
        _ = await _seed(scenario, f"Colleague {index}", tenant=scenario.target_id, creator_id=scenario.genesis.id)
    return await _seed(scenario, "Incoming")


async def test_destination_company_agent_cap_refuses_the_batch_although_the_applicants_own_cap_has_room(
    transfer_scenario: AdmissionScenario, revocations: list[UUID]
):
    scenario = transfer_scenario
    incoming = await _fill_destination_with_colleagues_agents(scenario)
    response = await _approve(scenario, TransferChoice.PAID)
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "transfer_destination_quota_exceeded",
        "message": (
            "The destination company has used all 2 agents. "
            "Its org admin can buy extra agents on the Users page in the admin console."
        ),
    }
    assert await _tenant_of(scenario, incoming) == scenario.source_id
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert revocations == []


async def test_purchased_extra_agents_let_the_destination_take_the_batch(
    transfer_scenario: AdmissionScenario, revocations: list[UUID], monkeypatch: pytest.MonkeyPatch
):
    scenario = transfer_scenario
    grant_extras(monkeypatch, agents=1)
    incoming = await _fill_destination_with_colleagues_agents(scenario)
    response = await _approve(scenario, TransferChoice.PAID)
    assert response.status_code == 200, response.text
    assert await _tenant_of(scenario, incoming) == scenario.target_id
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id


async def test_admission_into_a_company_with_no_free_account_is_refused_and_rolls_everything_back(
    transfer_scenario: AdmissionScenario, revocations: list[UUID]
):
    scenario = transfer_scenario
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)  # 5 accounts included
    while (await company_usage(scenario.db, scenario.target_id))[0] < 5:
        _ = await user_dao.create(
            obj_in={"display_name": "Filler", "tenant_id": scenario.target_id, "role": "member", "is_active": True}
        )
    incoming = await _seed(scenario, "Incoming")
    with pytest.raises(CompanyAccountLimitReachedError) as refused:
        _ = await _approve(scenario, TransferChoice.PAID)
    assert (refused.value.limit, refused.value.used) == (5, 5)
    assert await _tenant_of(scenario, incoming) == scenario.source_id
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    status = await scenario.db.fetchone("SELECT status FROM company_admission_requests")
    assert status == {"status": "pending"}


async def test_untiered_destination_and_free_source_disposition_are_not_capped(
    transfer_scenario: AdmissionScenario, revocations: list[UUID]
):
    scenario = transfer_scenario
    agents = [await _seed(scenario, f"Many {n}") for n in range(4)]
    response = await _approve(scenario, TransferChoice.PAID)
    assert response.status_code == 200, response.text
    assert {await _tenant_of(scenario, agent_id) for agent_id in agents} == {scenario.target_id}
    assert set(revocations) == set(agents)


async def test_free_disposition_into_a_tiered_destination_seals_instead_of_migrating(
    transfer_scenario: AdmissionScenario, revocations: list[UUID]
):
    scenario = transfer_scenario
    _ = await approve_admission_for(scenario.db, scenario.target_id, OrganizationPlanId.PRO)
    agents = [await _seed(scenario, f"Sealed {n}") for n in range(4)]
    response = await _approve(scenario, TransferChoice.FREE)
    assert response.status_code == 200, response.text
    assert {await _tenant_of(scenario, agent_id) for agent_id in agents} == {scenario.source_id}
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
