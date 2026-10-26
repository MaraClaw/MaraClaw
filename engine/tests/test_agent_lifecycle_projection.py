from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg.types.json import Jsonb

import test_agent_start_postgres as fixtures
from app.api.agent_lifecycle import router
from app.core.security import get_current_user, load_user_principal
from app.dao.agent_setup_mcp_dao import AGENT_SETUP_MCP_WORK_DDL
from app.schemas.agent_lifecycle import AgentLifecycleOut
from app.services.agent_manager import agent_manager
from app.services.agent_start_store import PostgresStartStore
from test_openclaw_update_postgres import agent, member, publish, runs, tenant

db = fixtures.db
start_db = fixtures.start_db


@pytest.fixture
async def lifecycle_db(start_db):
    await start_db.execute(AGENT_SETUP_MCP_WORK_DDL)
    return start_db


@pytest.fixture
def no_docker(monkeypatch):
    class ForbiddenDocker:
        def __getattr__(self, _name):
            pytest.fail("A database-only lifecycle read attempted a Docker operation")

    monkeypatch.setattr(agent_manager, "docker_client", ForbiddenDocker())


async def get_lifecycle(agent_id, user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lifecycle.test") as client:
        return await client.get(f"/api/agents/{agent_id}/lifecycle")


async def succeeded_setup(db, record):
    await db.execute(
        "INSERT INTO agent_setups(agent_id,inputs,state) VALUES (%(id)s,%(inputs)s,'succeeded')",
        {
            "id": record.id,
            "inputs": Jsonb({"gateway_key_encrypted": "secret-setup-key", "mcp_servers": ["unrecorded"]}),
        },
    )


async def test_manager_reads_committed_safe_projection_without_docker(lifecycle_db, no_docker):
    # Given durable start/MCP failures with secret-bearing setup and runtime-only state.
    operation, record = await fixtures.pending_start(lifecycle_db)
    await succeeded_setup(lifecycle_db, record)
    store = PostgresStartStore()
    assert await store.reserve(operation)
    await store.fail(operation, "configuration_failed")
    await lifecycle_db.execute(
        "INSERT INTO agent_setup_mcp_work(agent_id,server_id,owner_id,tenant_id,requester_id,state,warning_code,attempts) "
        "VALUES (%(id)s,'recorded',%(owner)s,%(tenant)s,%(owner)s,'warning','tools_unverified',2)",
        {"id": record.id, "owner": record.creator_id, "tenant": record.tenant_id},
    )
    await lifecycle_db.execute(
        "UPDATE agents SET api_key_hash='secret-api-hash',openclaw_image_id='secret-image' WHERE id=%(id)s",
        {"id": record.id},
    )
    user = await load_user_principal(record.creator_id)
    # When the real ASGI route reads current database state.
    response = await get_lifecycle(record.id, user)
    # Then only the declared safe slices are returned and optional results come from committed rows.
    assert response.status_code == 200
    body = AgentLifecycleOut.model_validate_json(response.content)
    offset = body.as_of.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert body.runtime.recorded_status == "stopped"
    assert body.runtime.recorded_container_id is None
    assert body.start is not None
    assert (body.start.operation_id, body.start.state, body.start.attempts) == (operation.operation_id, "pending", 1)
    assert body.start.last_attempt_at is not None
    assert body.start.next_retry_at is None
    assert body.start.last_failure_code == "configuration_failed"
    assert response.json()["optional_mcp"] == [
        {
            "server_id": "recorded",
            "state": "warning",
            "warning_code": "tools_unverified",
            "attempts": 2,
            "retryable": True,
        }
    ]
    assert response.json()["availability"] == {
        "policy_eligible": True,
        "blocking_codes": ["runtime_stopped"],
        "can_start": True,
        "can_retry_setup": False,
        "can_retry_mcp": True,
    }
    for private in ("secret-setup-key", "secret-api-hash", "secret-image", "unrecorded", "lease_until", "claim_token"):
        assert private not in response.text


@pytest.mark.parametrize("access", ["use", "foreign"])
async def test_use_only_and_foreign_tenant_cannot_read_projection(lifecycle_db, no_docker, access):
    # Given a company-use agent and either another company member or a foreign company administrator.
    _operation, record = await fixtures.pending_start(lifecycle_db)
    company = record.tenant_id if access == "use" else await tenant(lifecycle_db)
    assert company is not None
    requester = await member(lifecycle_db, company, role="member" if access == "use" else "org_admin")
    user = await load_user_principal(requester)
    # When that principal calls the real lifecycle endpoint.
    response = await get_lifecycle(record.id, user)
    # Then management authority is required before any projection is exposed.
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_lifecycle_manage_required"


@pytest.mark.parametrize("subject", ["user", "identity", "tenant", "membership"])
async def test_projection_reloads_principal_after_authenticated_snapshot_changes(lifecycle_db, no_docker, subject):
    # Given an authenticated stale snapshot of a manager whose authoritative principal changes.
    _operation, record = await fixtures.pending_start(lifecycle_db)
    assert record.tenant_id is not None
    manager = await member(lifecycle_db, record.tenant_id, role="org_admin")
    stale_user = await load_user_principal(manager)
    match subject:
        case "user":
            await lifecycle_db.execute("UPDATE users SET is_active=false WHERE id=%(id)s", {"id": manager})
        case "identity":
            await lifecycle_db.execute(
                "UPDATE identities SET is_active=false WHERE id=(SELECT identity_id FROM users WHERE id=%(id)s)",
                {"id": manager},
            )
        case "tenant":
            await lifecycle_db.execute("UPDATE tenants SET is_active=false WHERE id=%(id)s", {"id": record.tenant_id})
        case "membership":
            other = await tenant(lifecycle_db)
            await lifecycle_db.execute(
                "UPDATE users SET tenant_id=%(tenant)s WHERE id=%(id)s", {"tenant": other, "id": manager}
            )
    # When a stale credential dependency reaches the fresh database-only projection service.
    response = await get_lifecycle(record.id, stale_user)
    # Then stale identity, membership and tenant authority never authorize the read.
    assert response.status_code == (401 if subject in ("user", "identity") else 403)


async def test_projection_rechecks_revoked_manage_permission(lifecycle_db, no_docker):
    # Given a manager who loses a direct grant after their authenticated snapshot was loaded.
    _operation, record = await fixtures.pending_start(lifecycle_db)
    assert record.tenant_id is not None
    manager = await member(lifecycle_db, record.tenant_id)
    user = await load_user_principal(manager)
    await lifecycle_db.execute("UPDATE agents SET access_mode='custom' WHERE id=%(id)s", {"id": record.id})
    await lifecycle_db.execute(
        "INSERT INTO agent_permissions(id,agent_id,scope_type,scope_id,access_level) "
        "VALUES (%(id)s,%(agent)s,'user',%(user)s,'manage')",
        {"id": uuid4(), "agent": record.id, "user": manager},
    )
    assert (await get_lifecycle(record.id, user)).status_code == 200
    await lifecycle_db.execute("DELETE FROM agent_permissions WHERE agent_id=%(id)s", {"id": record.id})
    # When the same authenticated principal reads lifecycle again.
    response = await get_lifecycle(record.id, user)
    # Then the fresh permission computation denies the now-use-only principal.
    assert response.status_code == 403


async def test_historical_missing_optional_results_are_not_reconstructed(lifecycle_db, no_docker):
    # Given required setup inputs name servers but no durable optional outcomes exist.
    _operation, record = await fixtures.pending_start(lifecycle_db)
    await succeeded_setup(lifecycle_db, record)
    user = await load_user_principal(record.creator_id)
    # When lifecycle is read without initiating any work.
    response = await get_lifecycle(record.id, user)
    # Then no optional success, warning or retry is fabricated from replay input.
    assert response.status_code == 200
    assert response.json()["optional_mcp"] == []
    assert not response.json()["availability"]["can_retry_mcp"]
    assert await lifecycle_db.fetchval("SELECT count(*) FROM agent_setup_mcp_work") == 0


async def test_upgrade_projection_reuses_safe_contract_and_disables_mutation_capabilities(lifecycle_db, no_docker):
    # Given an active upgrade selected by the existing update service.
    _operation, record = await fixtures.pending_start(lifecycle_db)
    notice, _created = await publish(True)
    run = await runs.create_pending(notice_id=notice.id, agent_id=record.id, requested_by=record.creator_id)
    assert run is not None
    user = await load_user_principal(record.creator_id)
    # When the lifecycle route is read during upgrade contention.
    response = await get_lifecycle(record.id, user)
    # Then the existing safe upgrade shape is retained and Start is gated without a Docker observation.
    assert response.status_code == 200
    body = AgentLifecycleOut.model_validate_json(response.content)
    assert body.upgrade.run is not None
    assert (body.upgrade.run.id, body.upgrade.run.state, body.upgrade.run.failure_code) == (run.id, "pending", None)
    assert body.upgrade.required
    assert body.upgrade.can_upgrade
    assert "upgrade_in_progress" in body.availability.blocking_codes
    assert not body.availability.can_start
    assert not body.availability.can_retry_setup
    assert not body.availability.can_retry_mcp


async def test_policy_blockers_are_not_confused_with_recorded_runtime(lifecycle_db, no_docker):
    # Given a running agent with an inactive owner and active same-tenant manager.
    company = await tenant(lifecycle_db)
    owner = await member(lifecycle_db, company, active=False)
    manager = await member(lifecycle_db, company, role="org_admin")
    identifier = await agent(lifecycle_db, company, owner)
    user = await load_user_principal(manager)
    # When the read projection checks policy separately from recorded runtime.
    response = await get_lifecycle(identifier, user)
    # Then recorded running is not represented as policy availability or live readiness.
    assert response.status_code == 200
    assert response.json()["runtime"]["recorded_status"] == "running"
    assert response.json()["availability"]["blocking_codes"] == ["owner_inactive"]
    assert not response.json()["availability"]["policy_eligible"]
    assert not response.json()["availability"]["can_start"]
