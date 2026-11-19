import asyncio
import base64
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.core.security import encrypt_data
from app.dao import agent_dao, agent_setup_dao, user_dao
from app.db.connection import DbConnection
from app.db.pool import EnginePool, close_pool, init_pool
from app.db.session import connection_ctx
from app.records.agent_setup import AgentSetupInputs
from app.scripts import bootstrap_db
from app.services import agent_setup
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.fixture
async def setup_db(monkeypatch, tmp_path) -> AsyncIterator[tuple[DbConnection, UUID]]:
    conninfo = os.environ.get("AGENT_SETUP_TEST_DATABASE_URL", "")
    if not conninfo or os.environ.get("AGENT_SETUP_TEST_DATABASE_DISPOSABLE") != "1":
        pytest.skip("Explicit disposable AGENT_SETUP_TEST_DATABASE_URL approval is required")
    schema = f"setup_qa_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")

            async def open_pool() -> EnginePool:
                return await init_pool(conninfo=isolated, min_size=1, max_size=4)

            monkeypatch.setattr(bootstrap_db, "init_pool", open_pool)
            await bootstrap_db.main()
            await open_pool()
            actor = uuid4()
            await db.execute("INSERT INTO users (id, display_name) VALUES (%(id)s, 'Setup QA')", {"id": actor})
            storage = LocalStorageBackend(str(tmp_path))
            monkeypatch.setattr("app.services.agent_workspace.get_storage_backend", lambda: storage)
            monkeypatch.setattr(agent_setup.agent_manager, "_agent_dir", lambda aid: tmp_path / str(aid))
            yield db, actor
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def create_pending(actor: UUID, agent_id: UUID | None = None) -> UUID:
    inputs = AgentSetupInputs(
        files={"memory/recovered.md": base64.b64encode(b"durable snapshot").decode()},
        gateway_key_encrypted=encrypt_data("oc-durable", agent_setup.get_settings().SECRET_KEY),
    )
    async with connection_ctx():
        agent = await agent_dao.create(
            obj_in={
                "id": agent_id or uuid4(),
                "name": "Setup QA",
                "creator_id": actor,
                "setup_pending": True,
                "status": "idle",
            }
        )
        await agent_setup_dao.create(obj_in={"agent_id": agent.id, "inputs": inputs.model_dump(mode="json")})
    return agent.id


async def new_setup_agent(db: DbConnection, actor: UUID) -> tuple[UUID, UUID]:
    """A never-started agent owned by an active creator in an active tenant; returns (agent id, tenant id).

    Its status is `idle`, as creation records it; a setup retry is what moves a failed agent to `creating`.
    """
    identity_id, tenant_id = uuid4(), uuid4()
    await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity_id})
    await db.execute(
        "INSERT INTO tenants (id, name, slug, tier) VALUES (%(id)s, 'Start QA', 'start-qa', 'ultra')", {"id": tenant_id}
    )
    await db.execute(
        "UPDATE users SET identity_id = %(identity)s, tenant_id = %(tenant)s WHERE id = %(id)s",
        {"identity": identity_id, "tenant": tenant_id, "id": actor},
    )
    agent_id = await create_pending(actor)
    await db.execute(
        "UPDATE agents SET status = 'idle', tenant_id = %(tenant)s WHERE id = %(id)s",
        {"id": agent_id, "tenant": tenant_id},
    )
    return agent_id, tenant_id


async def finish_next_setup(failure_code: str | None = None) -> None:
    claim = await agent_setup_dao.claim()
    assert claim is not None
    assert await agent_setup_dao.finish(claim, failure_code)


async def start_operations(db: DbConnection, agent_id: UUID) -> list[DictRow]:
    return await db.fetchall("SELECT * FROM agent_start_operations WHERE agent_id = %(id)s", {"id": agent_id})


async def agent_state(db: DbConnection, agent_id: UUID) -> DictRow | None:
    return await db.fetchone("SELECT status, setup_pending FROM agents WHERE id = %(id)s", {"id": agent_id})


async def test_postgres_competing_workers_claim_only_once(setup_db):
    _db, actor = setup_db
    agent_id = await create_pending(actor)
    claims = await asyncio.gather(agent_setup_dao.claim(), agent_setup_dao.claim())
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].agent_id == agent_id
    assert await agent_setup_dao.owns(winners[0])


async def test_postgres_crashed_claim_recovery_fences_old_owner_and_finishes(setup_db, tmp_path):
    db, actor = setup_db
    agent_id = await create_pending(actor)
    old = await agent_setup_dao.claim()
    assert old is not None
    await db.execute(
        "UPDATE agent_setups SET lease_until = clock_timestamp() - interval '1 second' WHERE agent_id = %(id)s",
        {"id": agent_id},
    )
    recovered = await agent_setup_dao.claim()
    assert recovered is not None
    assert recovered.claim_token != old.claim_token
    assert not await agent_setup_dao.renew(old)
    assert not await agent_setup_dao.finish(old, "setup_failed")
    await agent_setup.run_agent_setup(recovered)
    row = await agent_setup_dao.get(agent_id)
    agent = await agent_dao.get(agent_id)
    assert row is not None
    assert row.state == "succeeded"
    assert agent is not None
    assert not agent.setup_pending
    assert (tmp_path / str(agent_id) / "memory/recovered.md").read_bytes() == b"durable snapshot"
    assert (tmp_path / str(agent_id) / ".maraclaw-gateway-key").read_text() == "oc-durable"
    assert not await agent_setup_dao.finish(old, "setup_failed")


async def test_postgres_expired_owner_cannot_finish_or_release_even_without_replacement(setup_db):
    db, actor = setup_db
    agent_id = await create_pending(actor)
    claim = await agent_setup_dao.claim()
    assert claim is not None
    await db.execute(
        "UPDATE agent_setups SET lease_until = clock_timestamp() - interval '1 second' WHERE agent_id = %(id)s",
        {"id": agent_id},
    )
    assert not await agent_setup_dao.finish(claim)
    assert not await agent_setup_dao.renew(claim)
    await agent_setup_dao.release(claim)
    row = await agent_setup_dao.get(agent_id)
    assert row is not None
    assert row.state == "running"
    assert not await agent_setup_dao.owns(claim)


async def test_postgres_live_owner_renews_lease(setup_db):
    db, actor = setup_db
    agent_id = await create_pending(actor)
    claim = await agent_setup_dao.claim()
    assert claim is not None
    await db.execute(
        "UPDATE agent_setups SET lease_until = clock_timestamp() + interval '3 seconds' WHERE agent_id = %(id)s",
        {"id": agent_id},
    )
    assert await agent_setup_dao.renew(claim)
    assert await db.fetchval(
        "SELECT lease_until > clock_timestamp() + interval '299 seconds' FROM agent_setups WHERE agent_id = %(id)s",
        {"id": agent_id},
    )


async def test_postgres_failure_is_stable_and_blocks_start(setup_db):
    _db, actor = setup_db
    agent_id = await create_pending(actor)
    claim = await agent_setup_dao.claim()
    assert claim is not None
    assert await agent_setup_dao.finish(claim, "setup_failed")
    assert await agent_setup_dao.claim() is None
    row = await agent_setup_dao.get(agent_id)
    agent = await agent_dao.get(agent_id)
    assert row is not None
    assert row.failure_code == "setup_failed"
    assert agent is not None
    assert agent.status == "error"
    assert agent.setup_pending
    with pytest.raises(ValueError, match="setup is incomplete"):
        await agent_setup.agent_manager.start_container(agent)


async def test_postgres_delete_cascades_work_and_late_claim_cannot_publish(setup_db, tmp_path):
    db, actor = setup_db
    agent_id = await create_pending(actor)
    claim = await agent_setup_dao.claim()
    assert claim is not None
    await db.execute("DELETE FROM agents WHERE id = %(id)s", {"id": agent_id})
    await agent_setup.run_agent_setup(claim)
    assert await agent_setup_dao.get(agent_id) is None
    assert not (tmp_path / str(agent_id)).exists()


async def test_postgres_agent_and_setup_rollback_together(setup_db):
    db, actor = setup_db
    agent_id = uuid4()

    async def abort():
        async with connection_ctx():
            await create_pending(actor, agent_id)
            raise RuntimeError("request aborted")

    with pytest.raises(RuntimeError, match="request aborted"):
        await abort()
    assert await db.fetchval("SELECT count(*) FROM agents WHERE id = %(id)s", {"id": agent_id}) == 0
    assert await db.fetchval("SELECT count(*) FROM agent_setups WHERE agent_id = %(id)s", {"id": agent_id}) == 0


async def test_postgres_http_create_survives_api_exit_and_worker_recovery(setup_db, tmp_path):
    from app.api import agents

    _db, actor_id = setup_db
    actor = await user_dao.get(actor_id)
    assert actor is not None
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: actor
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://setup-qa") as client:
            response = await client.post("/api/agents/", json={"name": "Durable HTTP", "permission_scope_type": "user"})
            assert response.status_code == 201, response.text
            accepted = response.json()
            agent_id = UUID(accepted["id"])
            blocked = await client.post(f"/api/agents/{agent_id}/start")
            assert blocked.status_code == 409
        assert not (tmp_path / str(agent_id)).exists()
        claim = await agent_setup_dao.claim()
        assert claim is not None
        await agent_setup.run_agent_setup(claim)
        row = await agent_setup_dao.get(agent_id)
        assert row is not None
        assert row.state == "succeeded"
        assert (tmp_path / str(agent_id) / ".maraclaw-gateway-key").read_text() == accepted["api_key"]
        agent = await agent_dao.get(agent_id)
        assert agent is not None
        assert not agent.setup_pending
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://setup-qa") as client:
            fetched = await client.get(f"/api/agents/{agent_id}")
        assert fetched.status_code == 200
        assert "api_key" not in fetched.json()
    finally:
        app.dependency_overrides.clear()


async def test_postgres_setup_success_queues_first_start(setup_db):
    db, actor = setup_db
    agent_id, tenant_id = await new_setup_agent(db, actor)
    await finish_next_setup()
    operations = await start_operations(db, agent_id)
    assert len(operations) == 1
    operation = operations[0]
    assert operation["state"] == "pending"
    assert operation["expected_container_id"] is None
    assert (operation["creator_id"], operation["tenant_id"]) == (actor, tenant_id)
    due = await db.fetchval(
        "SELECT next_retry_at <= now() FROM agent_start_retry_metadata WHERE operation_id = %(op)s",
        {"op": operation["operation_id"]},
    )
    assert due is True
    assert await agent_state(db, agent_id) == {"status": "idle", "setup_pending": False}


async def test_postgres_setup_failure_queues_no_start(setup_db):
    db, actor = setup_db
    agent_id, _tenant_id = await new_setup_agent(db, actor)
    await finish_next_setup("setup_failed")
    assert await start_operations(db, agent_id) == []
    assert await agent_state(db, agent_id) == {"status": "error", "setup_pending": True}


@pytest.mark.parametrize(("status", "container"), [("stopped", None), ("idle", "existing-container")])
async def test_postgres_setup_success_queues_no_start_outside_first_creation(setup_db, status, container):
    db, actor = setup_db
    agent_id, _tenant_id = await new_setup_agent(db, actor)
    await db.execute(
        "UPDATE agents SET status = %(status)s::agent_status_enum, container_id = %(container)s WHERE id = %(id)s",
        {"id": agent_id, "status": status, "container": container},
    )
    await finish_next_setup()
    assert await start_operations(db, agent_id) == []
    assert await agent_state(db, agent_id) == {"status": status, "setup_pending": False}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET is_active = false WHERE id = (SELECT creator_id FROM agents WHERE id = %(id)s)",
        "UPDATE agents SET is_expired = true WHERE id = %(id)s",
    ],
    ids=["inactive_creator", "expired_agent"],
)
async def test_postgres_setup_success_queues_no_start_for_ineligible_agent(setup_db, statement):
    db, actor = setup_db
    agent_id, _tenant_id = await new_setup_agent(db, actor)
    await db.execute(statement, {"id": agent_id})
    await finish_next_setup()
    assert await start_operations(db, agent_id) == []
    assert await agent_state(db, agent_id) == {"status": "idle", "setup_pending": False}


async def test_postgres_retried_setup_queues_first_start_once_it_succeeds(setup_db):
    db, actor = setup_db
    agent_id, _tenant_id = await new_setup_agent(db, actor)
    await finish_next_setup("setup_failed")
    assert await agent_setup_dao.retry_failed(agent_id) is not None
    assert await start_operations(db, agent_id) == []
    await finish_next_setup()
    operations = await start_operations(db, agent_id)
    assert [operation["state"] for operation in operations] == ["pending"]
    assert await agent_state(db, agent_id) == {"status": "idle", "setup_pending": False}
