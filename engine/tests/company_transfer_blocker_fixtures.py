from pathlib import Path
from uuid import UUID

import pytest

from app.schemas.company_admission import PendingAdmissionResponse
from app.services import (
    company_transfer_cleanup,
    company_transfer_fence,
    company_transfer_guards as guards,
    company_transfer_requests as requests,
)
from company_admission_fixtures import AdmissionScenario, admission_scenario
from test_company_transfer_postgres import transfer_scenario
from test_company_transfer_retention_postgres import seed_retained_data

__all__ = ["admission_scenario", "transfer_scenario"]


@pytest.fixture
def isolated_transfer_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def no_runtime() -> None:
        return None

    monkeypatch.setattr(company_transfer_fence.agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    monkeypatch.setattr(company_transfer_cleanup, "reconcile_transfer_cleanup", no_runtime)
    monkeypatch.setattr(guards, "get_transfer_policy", requests.get_transfer_policy)


async def submit_free(scenario: AdmissionScenario) -> UUID:
    response = await scenario.client.post(
        "/api/tenants/transfer",
        headers=scenario.headers(scenario.applicant),
        json={
            "password": "qa-password",
            "invitation_code": "JOIN",
            "transfer_choice": "free",
            "free_destructive_consent": True,
        },
    )
    assert response.status_code == 202, response.text
    return PendingAdmissionResponse.model_validate_json(response.content).request.id


@pytest.fixture
async def sealed_transfer(
    transfer_scenario: AdmissionScenario, isolated_transfer_runtime: None
) -> tuple[AdmissionScenario, UUID]:
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    admission_id = await submit_free(scenario)
    response = await scenario.client.post(
        f"/api/company-admissions/{admission_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert response.status_code == 200, response.text
    return scenario, agent_id
