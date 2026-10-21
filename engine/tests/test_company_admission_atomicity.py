from unittest.mock import AsyncMock

import anyio
import pytest

from app.dao.tenant_dao import tenant_dao
from app.services import company_admission_decisions, company_admissions
from company_admission_fixtures import AdmissionScenario, admission_scenario

__all__ = ["admission_scenario"]


async def test_notification_failure_rolls_back_request(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    monkeypatch.setattr(
        company_admissions, "send_notification", AsyncMock(side_effect=RuntimeError("injected notification failure"))
    )
    with pytest.raises(RuntimeError, match="injected notification failure"):
        await scenario.submit()
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_notification_failure_rolls_back_approval(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = admission_scenario
    request_id = await scenario.submit()
    monkeypatch.setattr(
        company_admission_decisions,
        "send_notification",
        AsyncMock(side_effect=RuntimeError("injected notification failure")),
    )
    with pytest.raises(RuntimeError, match="injected notification failure"):
        await scenario.client.post(
            f"/api/company-admissions/{request_id}/decision",
            json={"decision": "approved"},
            headers=scenario.headers(scenario.genesis),
        )
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"
    assert await scenario.db.fetchval("SELECT used_count FROM invitation_codes WHERE code='JOIN'") == 0
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


async def test_parallel_duplicate_requests_are_serialized(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    results: list[str] = []

    async def submit() -> None:
        results.append(await scenario.submit())

    async with anyio.create_task_group() as group:
        group.start_soon(submit)
        group.start_soon(submit)
    assert len(set(results)) == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications") == 1


async def test_fresh_demoted_genesis_cannot_decide(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    await scenario.db.execute("UPDATE users SET role='member' WHERE id=%(id)s", {"id": scenario.genesis.id})
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 403
    assert await scenario.db.fetchval("SELECT status FROM company_admission_requests") == "pending"


async def test_parallel_conflicting_decisions_have_one_winner(admission_scenario: AdmissionScenario):
    scenario = admission_scenario
    request_id = await scenario.submit()
    results: list[int] = []

    async def decide(decision: str) -> None:
        response = await scenario.client.post(
            f"/api/company-admissions/{request_id}/decision",
            json={"decision": decision},
            headers=scenario.headers(scenario.genesis),
        )
        results.append(response.status_code)

    async with anyio.create_task_group() as group:
        group.start_soon(decide, "approved")
        group.start_soon(decide, "rejected")
    assert sorted(results) == [200, 409]
    assert await scenario.db.fetchval("SELECT count(*) FROM notifications WHERE type='company_admission_resolved'") == 1


@pytest.mark.parametrize("tenant", ["source_id", "target_id"])
async def test_company_deletion_cleans_pending_admission(admission_scenario: AdmissionScenario, tenant: str):
    scenario = admission_scenario
    await scenario.submit()
    await tenant_dao.delete_cascade(getattr(scenario, tenant))
    assert await scenario.db.fetchval("SELECT count(*) FROM company_admission_requests") == 0
