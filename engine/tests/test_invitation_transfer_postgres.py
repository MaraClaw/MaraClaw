from uuid import UUID

import pytest

from app.dao.agent_dao import agent_dao
from company_admission_fixtures import AdmissionScenario, admission_scenario
from company_transfer_blocker_fixtures import isolated_transfer_runtime, transfer_scenario
from invitation_fixtures import used_count
from test_company_transfer_retention_postgres import seed_retained_data

__all__ = ["admission_scenario", "isolated_transfer_runtime", "transfer_scenario"]


async def transfer(scenario: AdmissionScenario, **fields: object):
    return await scenario.client.post(
        "/api/tenants/transfer",
        json={"invitation_code": "JOIN", "password": "qa-password", **fields},
        headers=scenario.headers(scenario.applicant),
    )


async def decide_payment(scenario: AdmissionScenario, transfer_id: UUID):
    return await scenario.client.post(
        f"/api/company-admissions/transfers/{transfer_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.outsider),
    )


async def nothing_moved(scenario: AdmissionScenario, agent_id: UUID) -> None:
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id
    assert await used_count(scenario) == 0
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    assert (agent.creator_id, agent.tenant_id) == (scenario.applicant.id, scenario.source_id)


@pytest.mark.parametrize(
    ("fields", "detail"),
    [
        ({}, "transfer_choice_required"),
        ({"transfer_choice": "free"}, "free_destructive_consent_required"),
        ({"transfer_choice": "free", "free_destructive_consent": False}, "free_destructive_consent_required"),
    ],
)
async def test_owner_of_agents_must_choose_and_consent_before_anything_moves(
    transfer_scenario: AdmissionScenario, isolated_transfer_runtime: None, fields: dict[str, object], detail: str
):
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)

    response = await transfer(scenario, **fields)

    assert response.status_code in {409, 422}
    assert response.json()["detail"]["code"] == detail
    await nothing_moved(scenario, agent_id)


async def test_free_choice_with_consent_admits_and_seals_agents_in_one_step(
    transfer_scenario: AdmissionScenario, isolated_transfer_runtime: None
):
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)

    response = await transfer(scenario, transfer_choice="free", free_destructive_consent=True)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    assert agent.creator_id != scenario.applicant.id
    assert await scenario.db.fetchval("SELECT count(*) FROM company_transfer_retained_agents") == 1
    assert await scenario.db.fetchone(
        "SELECT t.status AS transfer_status, a.status, a.decided_by_user_id FROM company_transfer_requests t "
        "JOIN company_admission_requests a ON a.id=t.admission_request_id"
    ) == {"transfer_status": "admission_created", "status": "approved", "decided_by_user_id": None}
    assert await used_count(scenario) == 1


async def test_paid_choice_waits_for_payment_then_admits_without_destination_approval(
    transfer_scenario: AdmissionScenario, isolated_transfer_runtime: None
):
    # Given a paid transfer awaiting payment review.
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    submitted = await transfer(scenario, transfer_choice="paid")
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "payment_review"
    transfer_id = UUID(submitted.json()["transfer_request"]["id"])
    await nothing_moved(scenario, agent_id)

    # When the payment reviewer confirms payment.
    confirmed = await decide_payment(scenario, transfer_id)

    # Then the member is already admitted with migrated agents, no genesis review, and a single spend.
    assert confirmed.status_code == 200, confirmed.text
    assert (await scenario.current_applicant()).tenant_id == scenario.target_id
    agent = await agent_dao.get(agent_id)
    assert agent is not None
    assert (agent.creator_id, agent.tenant_id) == (scenario.applicant.id, scenario.target_id)
    assert await used_count(scenario) == 1
    assert await scenario.db.fetchone("SELECT status, decided_by_user_id FROM company_admission_requests") == {
        "status": "approved",
        "decided_by_user_id": None,
    }
    types = {row["type"] for row in await scenario.db.fetchall("SELECT type FROM notifications")}
    assert "company_admission_pending" not in types
    # And repeating the confirmation neither admits again nor spends again.
    repeat = await decide_payment(scenario, transfer_id)
    assert repeat.json()["admission_request_id"] == confirmed.json()["admission_request_id"]
    assert await used_count(scenario) == 1


async def test_payment_confirmation_after_the_invitation_expired_changes_nothing(
    transfer_scenario: AdmissionScenario, isolated_transfer_runtime: None
):
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    submitted = await transfer(scenario, transfer_choice="paid")
    await scenario.db.execute("UPDATE invitation_codes SET expires_at=now() - interval '1 second'")

    response = await decide_payment(scenario, UUID(submitted.json()["transfer_request"]["id"]))

    assert response.status_code == 409
    await nothing_moved(scenario, agent_id)
    assert await scenario.db.fetchval("SELECT status FROM company_transfer_requests") == "payment_review"
