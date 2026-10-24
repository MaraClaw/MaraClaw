from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient, Response

from app.api import agents
from app.services import company_admission_decisions as decisions
from company_admission_fixtures import AdmissionScenario, admission_scenario
from company_transfer_blocker_fixtures import isolated_transfer_runtime, submit_free, transfer_scenario
from test_agent_create_departments import creation

__all__ = ["admission_scenario", "creation", "isolated_transfer_runtime", "transfer_scenario"]


@pytest.mark.parametrize("tenantless", [False, True])
async def test_platform_admin_can_override_target_without_departments(creation, tenantless: bool) -> None:
    # Given a platform admin whose membership is not the requested target.
    store, client = creation
    store.actor = replace(store.actor, role="platform_admin", tenant_id=None if tenantless else uuid4())
    target = uuid4()
    # When creating with an explicit target and no department grants.
    response = await client.post("/api/agents/", json={"name": "Override", "tenant_id": str(target)})
    # Then the authorized override is preserved.
    assert response.status_code == 201, response.text
    assert store.agent_rows[0].tenant_id == target


@pytest.mark.parametrize("reviewer", ["additional", "outsider", "applicant"])
async def test_unauthorized_reviewer_cannot_fence_agents(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, reviewer: str
) -> None:
    # Given a pending request and a busy runtime that must remain untouched by unauthorized reviewers.
    scenario = admission_scenario
    request_id = await scenario.submit()
    fence = AsyncMock(side_effect=HTTPException(423, "Runtime is busy"))
    monkeypatch.setattr(decisions, "fence_owned_agents", fence)
    # When a non-genesis reviewer attempts approval.
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(getattr(scenario, reviewer)),
    )
    # Then authorization wins before even attempting a flock.
    assert response.status_code == 403, response.text
    fence.assert_not_awaited()


async def test_genesis_is_rechecked_after_fencing(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a genesis reviewer who loses authority between preliminary authorization and DB locks.
    scenario = admission_scenario
    request_id = await scenario.submit()

    async def demote(_applicant) -> frozenset[UUID]:
        await scenario.db.execute("UPDATE users SET is_genesis=false WHERE id=%(id)s", {"id": scenario.genesis.id})
        return frozenset[UUID]()

    monkeypatch.setattr(decisions, "fence_owned_agents", demote)
    # When approval resumes under the row locks.
    response = await scenario.client.post(
        f"/api/company-admissions/{request_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    # Then stale authority cannot approve the transfer.
    assert response.status_code == 403, response.text
    assert (await scenario.current_applicant()).tenant_id == scenario.source_id


@pytest.mark.parametrize("racing", [False, True])
async def test_creation_rejects_stale_membership_after_final_approval_without_departments(
    transfer_scenario: AdmissionScenario,
    isolated_transfer_runtime: None,
    monkeypatch: pytest.MonkeyPatch,
    racing: bool,
) -> None:
    # Given a real pending transfer and an authenticated source-tenant principal.
    scenario = transfer_scenario
    admission_id = await submit_free(scenario)
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: scenario.applicant
    monkeypatch.setattr(agents, "check_agent_creation_quota", AsyncMock())
    monkeypatch.setattr(agents, "persist_agent_setup", AsyncMock())
    arrived, resume = anyio.Event(), anyio.Event()
    lock_tenants = agents.lock_tenants

    async def pause_before_locks(db, tenant_ids) -> None:
        arrived.set()
        await resume.wait()
        await lock_tenants(db, tenant_ids)

    if racing:
        monkeypatch.setattr(agents, "lock_tenants", pause_before_locks)
    responses: list[Response] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://creation.test") as client:

        async def create() -> None:
            responses.append(await client.post("/api/agents/", json={"name": "Stale creation", "department_ids": []}))
            arrived.set()

        # When final approval commits before the creation transaction acquires its tenant lock.
        with anyio.fail_after(15):
            async with anyio.create_task_group() as group:
                if racing:
                    group.start_soon(create)
                    await arrived.wait()
                approved = await scenario.client.post(
                    f"/api/company-admissions/{admission_id}/decision",
                    json={"decision": "approved"},
                    headers=scenario.headers(scenario.genesis),
                )
                assert approved.status_code == 200, approved.text
                resume.set()
                if not racing:
                    await create()
    # Then both a stale principal and the interleaving race fail without inserting a source agent.
    assert responses[0].status_code == 409, responses[0].text
    assert (
        await scenario.db.fetchval("SELECT count(*) FROM agents WHERE creator_id=%(id)s", {"id": scenario.applicant.id})
        == 0
    )
