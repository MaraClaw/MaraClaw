from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import agents, tenants
from app.core.security import get_current_user
from app.db.session import bind_crud_connection
from app.services import tenant_provisioning
from company_admission_fixtures import AdmissionScenario
from company_transfer_blocker_fixtures import (
    admission_scenario,
    isolated_transfer_runtime,
    sealed_transfer,
    transfer_scenario,
)

__all__ = ["admission_scenario", "isolated_transfer_runtime", "sealed_transfer", "transfer_scenario"]


@pytest.mark.parametrize("resource", ["agent", "target_tenant", "source_tenant"])
async def test_retained_transfer_blocks_deletion_before_teardown(
    sealed_transfer: tuple[AdmissionScenario, UUID], monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    # Given a sealed transfer with completed runtime cleanup and retained history.
    scenario, agent_id = sealed_transfer
    await scenario.db.execute("UPDATE company_transfer_runtime_cleanup SET state='completed'")
    if resource == "agent":
        await scenario.db.execute("UPDATE agents SET access_mode='company' WHERE id=%(id)s", {"id": agent_id})
    if resource == "source_tenant":
        await scenario.db.execute(
            "UPDATE tenants SET is_default_end_user_org=false WHERE id=%(id)s", {"id": scenario.source_id}
        )
    app = FastAPI()
    app.include_router(agents.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.include_router(tenants.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: scenario.outsider
    agent_teardown = AsyncMock(wraps=agents.snapshot_agent_teardown)
    tenant_teardown = AsyncMock(wraps=tenant_provisioning.snapshot_agent_teardown)
    monkeypatch.setattr(agents, "snapshot_agent_teardown", agent_teardown)
    monkeypatch.setattr(tenant_provisioning, "snapshot_agent_teardown", tenant_teardown)
    paths = {
        "agent": f"/api/agents/{agent_id}",
        "target_tenant": f"/api/tenants/{scenario.target_id}",
        "source_tenant": f"/api/tenants/{scenario.source_id}",
    }

    # When an authorized platform admin requests ordinary deletion.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.delete(paths[resource])

    # Then retention is explicit, no teardown starts, and the ledger/history survive.
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "transfer_retention_conflict"
    agent_teardown.assert_not_awaited()
    tenant_teardown.assert_not_awaited()
    assert await scenario.db.fetchval("SELECT count(*) FROM company_transfer_retained_agents") == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM chat_messages WHERE agent_id=%(id)s", {"id": agent_id}) == 1
    assert await scenario.db.fetchval("SELECT count(*) FROM tenants WHERE id=%(id)s", {"id": scenario.target_id}) == 1
