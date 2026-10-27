from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import linkup_proxy
from app.dao.agent_dao import agent_dao
from app.services.linkup.tokens import _proxy_token, make_proxy_token
from company_admission_fixtures import AdmissionScenario, admission_scenario
from company_transfer_blocker_fixtures import isolated_transfer_runtime, sealed_transfer, submit_free, transfer_scenario
from test_company_transfer_retention_postgres import seed_retained_data

__all__ = ["admission_scenario", "isolated_transfer_runtime", "sealed_transfer", "transfer_scenario"]


async def search(token: str):
    app = FastAPI()
    app.include_router(linkup_proxy.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://proxy.test") as client:
        return await client.post(
            "/api/linkup/v1/search", headers={"Authorization": f"Bearer {token}"}, json={"q": "qa"}
        )


@pytest.mark.parametrize("state", ["missing", "expired_flag", "elapsed", "active", "future"])
async def test_proxy_checks_current_agent_before_forwarding(
    admission_scenario: AdmissionScenario, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    # Given a correctly signed token for a missing, expired, or usable agent.
    scenario = admission_scenario
    agent_id = uuid4()
    if state != "missing":
        expires_at = None
        if state in {"elapsed", "future"}:
            expires_at = datetime.now(UTC) + timedelta(days=-1 if state == "elapsed" else 1)
        agent = await agent_dao.create(
            obj_in={
                "name": "Proxy agent",
                "creator_id": scenario.applicant.id,
                "tenant_id": scenario.source_id,
                "is_expired": state == "expired_flag",
                "expires_at": expires_at,
            }
        )
        agent_id = agent.id
    upstream = AsyncMock(return_value=(200, '{"results":[]}', {}))
    monkeypatch.setattr(linkup_proxy, "proxy_linkup", upstream)
    # When a guest presents the token.
    token = _proxy_token(agent_id) if state == "missing" else await make_proxy_token(agent_id)
    response = await search(token)
    # Then only live, unexpired agents can spend an upstream key.
    expected = 200 if state in {"active", "future"} else 401
    assert response.status_code == expected, response.text
    assert upstream.await_count == (1 if expected == 200 else 0)


async def test_free_approval_revokes_previously_usable_copied_proxy_token(
    transfer_scenario: AdmissionScenario, isolated_transfer_runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a source agent with a copied token that successfully reaches the proxy.
    scenario = transfer_scenario
    agent_id = await seed_retained_data(scenario)
    upstream = AsyncMock(return_value=(200, "{}", {}))
    monkeypatch.setattr(linkup_proxy, "proxy_linkup", upstream)
    token = await make_proxy_token(agent_id)
    assert (await search(token)).status_code == 200
    upstream.reset_mock()
    admission_id = await submit_free(scenario)
    approved = await scenario.client.post(
        f"/api/company-admissions/{admission_id}/decision",
        json={"decision": "approved"},
        headers=scenario.headers(scenario.genesis),
    )
    assert approved.status_code == 200, approved.text
    # When the copied token is replayed after committed final approval.
    response = await search(token)
    # Then it is revoked without an upstream request.
    assert response.status_code == 401, response.text
    upstream.assert_not_awaited()


async def test_retention_ledger_seals_proxy_even_if_expiry_flag_is_cleared(
    sealed_transfer: tuple[AdmissionScenario, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given retained history whose incidental expiry flag has been cleared.
    scenario, agent_id = sealed_transfer
    await scenario.db.execute("UPDATE agents SET is_expired=false, expires_at=NULL WHERE id=%(id)s", {"id": agent_id})
    upstream = AsyncMock(return_value=(200, "{}", {}))
    monkeypatch.setattr(linkup_proxy, "proxy_linkup", upstream)
    # When the old token is presented.
    token = await make_proxy_token(agent_id)
    response = await search(token)
    # Then the durable retention ledger still revokes access.
    assert response.status_code == 401, response.text
    upstream.assert_not_awaited()
