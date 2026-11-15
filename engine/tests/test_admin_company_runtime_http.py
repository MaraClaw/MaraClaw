from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, assert_never
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pywhales import DockerClient
from tests.test_admin_company_page_http import principal

from app.api.admin_company_runtime import router
from app.core.domain.agents import AgentStatus
from app.core.domain.users import UserRole
from app.dao.agent_lifecycle_projection_dao import AgentLifecycleProjections
from app.records.agent import AgentRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.services import admin_companies as cursors, admin_company_runtime as runtime
from app.services.agent_resource_pressure_docker import GuestInspection

__all__ = ["principal"]
_COMPANY = UUID(int=100)
_PATH = f"/api/admin/companies/{_COMPANY}/runtime"
_HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://inventory.test") as client:
        yield client


@pytest.fixture
def inventory(monkeypatch, principal):
    state = {"connection_open": False}

    @asynccontextmanager
    async def connection():
        state["connection_open"] = True
        try:
            yield None
        finally:
            state["connection_open"] = False

    monkeypatch.setattr(runtime, "connection_ctx", connection)
    monkeypatch.setattr(runtime.agent_manager, "docker_client", DockerClient())
    company = TenantRecord(id=_COMPANY, name="Company", slug="company", tier=OrganizationPlanId.PRO)
    principal.tenant_id = _COMPANY
    agents = [
        AgentRecord(
            id=UUID(int=number),
            name=f"Agent {number}",
            creator_id=principal.id,
            tenant_id=_COMPANY,
            status=AgentStatus.RUNNING,
            container_id=f"guest-{number}",
            api_key_hash="secret-hash",
        )
        for number in range(1, 5)
    ]
    reads = {
        "tenant": AsyncMock(return_value=company),
        "company_page": AsyncMock(return_value=agents),
        "many": AsyncMock(return_value=AgentLifecycleProjections()),
        "owners": AsyncMock(return_value={principal.id: principal}),
    }
    monkeypatch.setattr(runtime.company_admission_dao, "tenant", reads["tenant"])
    monkeypatch.setattr(runtime.agent_lifecycle_projection_dao, "company_page", reads["company_page"])
    monkeypatch.setattr(runtime.agent_lifecycle_projection_dao, "many", reads["many"])
    monkeypatch.setattr(runtime.user_dao, "get_many_with_identity", reads["owners"])
    guest = GuestInspection("guest-1", str(agents[0].id), False, 0, 0, True, None, "", 137, 7, status="exited")

    def inspect(_docker, references, timeout):
        assert not state["connection_open"]
        assert timeout == 5.0
        return {"guest-1": guest, "guest-2": replace(guest, owner=str(uuid4()))}

    inspection = Mock(side_effect=inspect)
    monkeypatch.setattr(runtime, "inspect_guests", inspection)
    return agents, reads, inspection


@pytest.mark.parametrize("kind", ["missing", "member", "org_admin", "force_change"])
async def test_authorization_precedes_reads_and_cursor_parsing(client, principal, inventory, kind):
    match kind:
        case "member":
            principal.role = UserRole.MEMBER
        case "org_admin":
            principal.role = UserRole.ORG_ADMIN
        case "force_change":
            principal.identity.must_change_password = True
    response = await client.get(_PATH, headers={} if kind == "missing" else _HEADERS, params={"cursor": "!"})
    assert response.status_code == (401 if kind == "missing" else 403)
    for read in inventory[1].values():
        read.assert_not_awaited()
    inventory[2].assert_not_called()


async def test_page_batches_reads_and_observes_after_database_scope(client, principal, inventory):
    agents, reads, inspection = inventory
    response = await client.get(_PATH, headers=_HEADERS, params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["company_id"] == str(_COMPANY)
    assert body["observation_available"]
    assert [item["agent_id"] for item in body["items"]] == [str(agent.id) for agent in agents[:3]]
    assert [item["observation"]["state"] for item in body["items"]] == ["observed", "foreign", "missing"]
    first = body["items"][0]
    assert first["recorded_status"] == "running"
    assert first["blocking_codes"] == []
    assert first["observation"]["container_status"] == "exited"
    assert first["observation"]["restart_count_since_last_start"] == 7
    assert first["observation"]["oom_killed_since_last_start"]
    assert body["items"][1]["observation"]["exit_code"] is None
    assert "secret-hash" not in response.text
    assert cursors.decode_runtime_cursor(body["next_cursor"], _COMPANY) == agents[2].id
    reads["many"].assert_awaited_once_with(None, [agent.id for agent in agents[:3]])
    reads["owners"].assert_awaited_once_with([principal.id])
    inspection.assert_called_once_with(runtime.agent_manager.docker_client, ["guest-1", "guest-2", "guest-3"], 5.0)


@pytest.mark.parametrize("docker_available", [True, False])
async def test_docker_unavailable_is_not_missing_and_absent_reference_needs_no_inspection(
    client,
    inventory,
    monkeypatch,
    docker_available,
):
    agents, reads, inspection = inventory
    inspection.side_effect = None
    inspection.return_value = None
    if not docker_available:
        monkeypatch.setattr(runtime.agent_manager, "docker_client", None)
    agents[0].container_id = None
    reads["company_page"].return_value = agents[:2]
    response = await client.get(_PATH, headers=_HEADERS)
    assert response.status_code == 200
    assert not response.json()["observation_available"]
    assert [item["observation"]["state"] for item in response.json()["items"]] == ["missing", "unavailable"]
    if docker_available:
        inspection.assert_called_once_with(runtime.agent_manager.docker_client, ["guest-2"], 5.0)
    else:
        inspection.assert_not_called()


@pytest.mark.parametrize("kind", ["malformed", "oversized", "company", "purpose", "forged"])
async def test_cursor_rejections_precede_inventory_reads(
    client, inventory, kind: Literal["malformed", "oversized", "company", "purpose", "forged"]
):
    match kind:
        case "malformed":
            cursor = "!"
        case "oversized":
            cursor = "a" * 1025
        case "company":
            cursor = cursors.encode_runtime_cursor(uuid4(), uuid4())
        case "purpose":
            cursor = cursors._encode_cursor(
                TenantRecord(
                    id=_COMPANY,
                    name="Company",
                    slug="company",
                    tier=OrganizationPlanId.PRO,
                    created_at=datetime.now(UTC),
                )
            )
        case "forged":
            cursor = urlsafe_b64encode(
                f'{{"version":1,"purpose":"company-runtime","company_id":"{_COMPANY}",'
                f'"agent_id":"{uuid4()}","signature":"{"0" * 64}"}}'.encode()
            ).decode()
        case unreachable:
            assert_never(unreachable)
    response = await client.get(_PATH, headers=_HEADERS, params={"cursor": cursor})
    assert response.status_code == 400
    for read in inventory[1].values():
        read.assert_not_awaited()


async def test_uuid_boundary_empty_page_and_limit_clamping(client, inventory):
    agents, reads, inspection = inventory
    reads["company_page"].return_value = []
    cursor = cursors.encode_runtime_cursor(_COMPANY, agents[0].id)
    response = await client.get(_PATH, headers=_HEADERS, params={"cursor": cursor, "limit": 999})
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert response.json()["next_cursor"] is None
    assert reads["company_page"].await_args.args[1].after == agents[0].id
    assert reads["company_page"].await_args.args[1].limit == 200
    inspection.assert_not_called()


async def test_unknown_company_returns_404_without_agent_or_docker_reads(client, inventory):
    inventory[1]["tenant"].return_value = None
    response = await client.get(_PATH, headers=_HEADERS)
    assert response.status_code == 404
    inventory[1]["company_page"].assert_not_awaited()
    inventory[2].assert_not_called()
