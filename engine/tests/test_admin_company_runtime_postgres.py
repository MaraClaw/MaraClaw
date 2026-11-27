from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pywhales import DockerClient
from tests.test_admin_company_stats_postgres import CompanyWorld, _tenant, _user, conninfo, statements, world

from app.api.admin_company_runtime import router
from app.dao.agent_dao import agent_dao
from app.dao.agent_runtime_loss_dao import agent_runtime_loss_dao
from app.dao.agent_workspace_usage_dao import agent_workspace_usage_dao
from app.db.session import get_connection
from app.schemas.admin_companies import CompanyRuntimePageOut
from app.services import admin_company_runtime as runtime
from app.services.agent_work_eligibility import agent_work_blocking_codes

__all__ = ["conninfo", "statements", "world"]


@pytest.fixture
async def client(world):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://inventory.test") as client:
        yield client


@pytest.fixture
def inspection(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.agent_manager, "docker_client", DockerClient())

    def inspect(_docker, references, timeout):
        assert get_connection() is None
        assert timeout == 5.0
        calls.append(references)
        return {}

    monkeypatch.setattr(runtime, "inspect_guests", inspect)
    return calls


async def _agents(world: CompanyWorld, count: int):
    company = await _tenant(datetime.now(UTC), "Runtime")
    owner = await _user(company, email=f"owner-{uuid4()}@x.test")
    agents = []
    for number in range(1, count + 1):
        agent = await agent_dao.create(
            obj_in={
                "id": UUID(int=number),
                "tenant_id": company,
                "creator_id": owner,
                "name": f"Agent {number}",
                "status": "running",
                "container_id": f"guest-{number}",
                "api_key_hash": "private-api-hash",
            }
        )
        agents.append(agent)
    return company, owner, agents


async def test_inventory_id_pages_batch_sql_and_owner_refresh(world, client, inspection, statements):
    company, owner, agents = await _agents(world, 9)
    foreign = await _tenant(datetime.now(UTC), "Foreign")
    await agent_dao.create(obj_in={"id": UUID(int=100), "tenant_id": foreign, "creator_id": owner, "name": "Foreign"})
    path = f"/api/admin/companies/{company}/runtime"
    # Compare page sizes with the same principal-cache state, excluding the first authentication fill.
    warmup = await client.get(path, headers=world.headers, params={"limit": 1})
    assert warmup.status_code == 200
    inspection.clear()
    counts = []
    observed = []
    for size in (1, 6):
        statements.clear()
        response = await client.get(path, headers=world.headers, params={"limit": size})
        assert response.status_code == 200
        body = CompanyRuntimePageOut.model_validate_json(response.content)
        counts.append(len(statements))
        assert len(body.items) == size
        assert (
            sum("FROM users u LEFT JOIN identities" in query and "ANY(%(ids)s)" in query for query in statements) == 1
        )
        assert sum("FROM agent_start_operations" in query for query in statements) == 1
        assert sum("FROM agent_runtime_losses" in query for query in statements) == 1
        assert sum("FROM agent_workspace_usage" in query for query in statements) == 1
        assert sum("FROM openclaw_update_runs" in query for query in statements) == 1
        assert not any("OFFSET" in query.upper() for query in statements)
    assert counts[0] == counts[1]
    assert len(inspection) == 2
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 4}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(path, headers=world.headers, params=params)
        assert response.status_code == 200
        page = CompanyRuntimePageOut.model_validate_json(response.content)
        observed.extend(item.agent_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert observed == [agent.id for agent in agents]
    await world.db.execute("UPDATE users SET is_active=false WHERE id=%(id)s", {"id": owner})
    refreshed = await client.get(path, headers=world.headers, params={"limit": 2})
    assert [item["blocking_codes"] for item in refreshed.json()["items"]] == [["owner_inactive"], ["owner_inactive"]]


@pytest.mark.parametrize("upgrade_state", ["pending", "cutover", "succeeded"])
async def test_inventory_projects_durable_concerns_and_policy_parity(world, client, inspection, upgrade_state):
    company, owner, agents = await _agents(world, 2)
    agent = agents[0]
    operation, notice, transfer = uuid4(), uuid4(), uuid4()
    await world.db.execute(
        "INSERT INTO agent_start_operations(agent_id,operation_id,state,creator_id,tenant_id) "
        "VALUES (%(agent)s,%(operation)s,'pending',%(owner)s,%(company)s)",
        {"agent": agent.id, "operation": operation, "owner": owner, "company": company},
    )
    await world.db.execute(
        "INSERT INTO agent_start_retry_metadata(operation_id,attempts,last_attempt_at,next_retry_at,last_failure_code) "
        "VALUES (%(operation)s,3,now(),now()+interval '1 minute','readiness_failed')",
        {"operation": operation},
    )
    await agent_runtime_loss_dao.record(
        world.db, agent.id, "container_missing", recorded_container_id="gone", image=None
    )
    await world.db.execute(
        "INSERT INTO agent_workspace_usage(agent_id,dir_bytes,layer_bytes,limit_bytes,measured_at,over_since) "
        "VALUES (%(agent)s,100,50,125,now(),now())",
        {"agent": agent.id},
    )
    await world.db.execute(
        "INSERT INTO openclaw_update_notices(id,request_id,title,body,mandatory) VALUES (%(id)s,%(id)s,'Update','',true)",
        {"id": notice},
    )
    await world.db.execute(
        "INSERT INTO openclaw_update_runs(id,notice_id,agent_id,state) VALUES (%(id)s,%(notice)s,%(agent)s,%(state)s)",
        {"id": uuid4(), "notice": notice, "agent": agent.id, "state": upgrade_state},
    )
    target = await _tenant(datetime.now(UTC), "Target")
    await world.db.execute(
        "INSERT INTO company_transfer_requests(id,applicant_user_id,source_tenant_id,target_tenant_id,choice,method,"
        "fee_amount_minor,currency,instructions) VALUES (%(id)s,%(owner)s,%(source)s,%(target)s,'paid','sso',0,'USD','')",
        {"id": transfer, "owner": owner, "source": company, "target": target},
    )
    await world.db.execute(
        "INSERT INTO company_transfer_runtime_cleanup(agent_id,transfer_request_id,choice) VALUES (%(agent)s,%(id)s,'paid')",
        {"agent": agent.id, "id": transfer},
    )
    expected = await agent_work_blocking_codes(agent)
    response = await client.get(f"/api/admin/companies/{company}/runtime", headers=world.headers)
    assert response.status_code == 200
    body = CompanyRuntimePageOut.model_validate_json(response.content)
    first, other = body.items
    assert first.runtime_loss is not None
    assert first.runtime_loss.reason == "container_missing"
    assert first.start is not None
    assert first.start.operation_id == operation
    assert first.start.attempts == 3
    assert first.start.last_failure_code == "readiness_failed"
    assert first.start.next_retry_at is not None
    assert first.upgrade_in_progress is (upgrade_state in ("pending", "cutover"))
    assert first.workspace is not None
    assert (first.workspace.used_bytes, first.workspace.limit_bytes) == (150, 125)
    assert first.workspace.over_since is not None
    assert first.blocking_codes == expected == ("transfer_pending",)
    assert first.observation.state == "missing"
    assert other.start is None
    assert other.workspace is None
    assert other.runtime_loss is None
    assert not other.upgrade_in_progress
    assert other.blocking_codes == ()
    assert "private-api-hash" not in response.text


async def test_inventory_returns_the_measurement_state_the_quota_dao_stored(world, client, inspection):
    company, _owner, agents = await _agents(world, 4)
    base = datetime(2026, 11, 15, 3, 0, 0, 123456, tzinfo=UTC)
    minute = timedelta(minutes=1)
    failed_after_measurement, never_measured, remeasure_pending, unmeasured = agents
    for agent in (failed_after_measurement, remeasure_pending):
        assert await agent_workspace_usage_dao.record_measurement(
            agent.id, dir_bytes=100, layer_bytes=50, limit_bytes=1000, started=base
        )
    assert await agent_workspace_usage_dao.record_walk_failure(
        failed_after_measurement.id, limit_bytes=1000, started=base + minute
    )
    assert await agent_workspace_usage_dao.request_remeasure(failed_after_measurement.id, requested=base + 2 * minute)
    assert await agent_workspace_usage_dao.record_walk_failure(never_measured.id, limit_bytes=1000, started=base)
    assert await agent_workspace_usage_dao.request_remeasure(remeasure_pending.id, requested=base + minute)
    response = await client.get(f"/api/admin/companies/{company}/runtime", headers=world.headers)
    assert response.status_code == 200
    items = {
        item.agent_id: item.workspace for item in CompanyRuntimePageOut.model_validate_json(response.content).items
    }
    expected = {
        failed_after_measurement.id: (150, base, base + minute, base + 2 * minute),
        never_measured.id: (0, None, base, None),
        remeasure_pending.id: (150, base, None, base + minute),
    }
    for agent_id, state in expected.items():
        workspace = items[agent_id]
        assert workspace is not None
        assert (
            workspace.used_bytes,
            workspace.measured_at,
            workspace.walk_failed_at,
            workspace.remeasure_requested_at,
        ) == state
        row = await agent_workspace_usage_dao.get(agent_id)
        assert row is not None
        assert (row.used_bytes, row.measured_at, row.walk_failed_at, row.remeasure_requested_at) == state
        assert (workspace.limit_bytes, workspace.over_since) == (1000, None)
    assert items[unmeasured.id] is None
