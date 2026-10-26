import asyncio
import base64
from collections import Counter
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import test_agent_setup_postgres
from app.api import agent_setup_mcp as api
from app.dao import agent_dao, agent_setup_dao, agent_template_dao, skill_dao, skill_file_dao, user_dao
from app.dao.agent_setup_mcp_dao import AGENT_SETUP_MCP_WORK_DDL, agent_setup_mcp_dao
from app.db.session import connection_ctx
from app.schemas.schemas import AgentCreate
from app.services import agent_setup, agent_setup_mcp_worker, resource_discovery

setup_db = test_agent_setup_postgres.setup_db


@pytest.fixture
async def lifecycle(setup_db, monkeypatch):
    """Permit destination-folder collisions only in this isolated policy test schema."""
    db, actor = setup_db
    await db.execute(AGENT_SETUP_MCP_WORK_DDL)
    await db.execute("ALTER TABLE skills DROP CONSTRAINT skills_folder_name_key")
    tenant, foreign, identity = uuid4(), uuid4(), uuid4()
    for tenant_id in (tenant, foreign):
        await db.execute(
            "INSERT INTO tenants (id,name,slug) VALUES (%(id)s,'QA',%(slug)s)",
            {"id": tenant_id, "slug": tenant_id.hex},
        )
    await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity})
    await db.execute(
        "UPDATE users SET tenant_id=%(tenant)s,identity_id=%(identity)s,role='platform_admin' WHERE id=%(actor)s",
        {"tenant": tenant, "identity": identity, "actor": actor},
    )
    monkeypatch.setattr(agent_setup, "hook_new_agent", AsyncMock())
    return db, actor, tenant, foreign, identity


async def skill(tenant, folder, content, *, default=False):
    record = await skill_dao.create(
        obj_in={
            "name": content,
            "folder_name": folder,
            "tenant_id": tenant,
            "is_default": default,
            "description": "",
            "category": "qa",
            "icon": "",
            "is_builtin": False,
        }
    )
    await skill_file_dao.create(obj_in={"skill_id": record.id, "path": "SKILL.md", "content": content})
    return record.id


async def provision(actor, tenant, *, explicit=(), folders=(), servers=()):
    async with connection_ctx():
        template = await agent_template_dao.create(
            obj_in={
                "name": "QA",
                "default_skills": list(folders),
                "default_mcp_servers": list(servers),
            }
        )
        agent = await agent_dao.create(
            obj_in={
                "name": "Lifecycle QA",
                "creator_id": actor,
                "tenant_id": tenant,
                "setup_pending": True,
                "status": "creating",
            }
        )
        await agent_setup.persist_agent_setup(
            agent,
            AgentCreate(name=agent.name, template_id=template.id, skill_ids=list(explicit)),
            "oc-lifecycle-secret",
        )
        return agent


@pytest.mark.parametrize("choice", ["implicit", "explicit_global", "explicit_tenant", "null_tenant", "folder"])
async def test_snapshot_resolves_only_destination_scope_with_deterministic_precedence(lifecycle, choice):
    db, actor, tenant, foreign, _identity = lifecycle
    global_id = await skill(None, "qa-shared", "GLOBAL", default=True)
    tenant_id = await skill(tenant, "qa-shared", "TENANT", default=choice != "folder")
    await skill(foreign, "qa-shared", "FOREIGN", default=True)
    await skill(foreign, "qa-foreign-only", "FOREIGN ONLY", default=True)
    await skill(foreign, "qa-foreign-folder", "FOREIGN FOLDER")
    explicit = (global_id,) if choice == "explicit_global" else (tenant_id,) if choice == "explicit_tenant" else ()
    agent = await provision(
        actor,
        None if choice == "null_tenant" else tenant,
        explicit=explicit,
        folders=("qa-shared", "qa-foreign-folder"),
    )
    row = await agent_setup_dao.get(agent.id)
    assert row is not None
    payload = {path: base64.b64decode(value) for path, value in row.inputs.files.items()}
    expected = b"GLOBAL" if choice in ("null_tenant", "explicit_global") else b"TENANT"
    assert payload["skills/qa-shared/SKILL.md"] == expected
    assert not any(b"FOREIGN" in content for content in payload.values())
    assert await db.fetchval("SELECT count(*) FROM agent_setup_mcp_work WHERE agent_id=%(id)s", {"id": agent.id}) == 0


@pytest.mark.parametrize("selection", ["foreign", "unknown", "duplicate_folder"])
async def test_invalid_explicit_selection_rolls_back_entire_creation(lifecycle, selection):
    db, actor, tenant, foreign, _identity = lifecycle
    foreign_id = await skill(foreign, "qa-secret", "PRIVATE FOREIGN")
    global_id = await skill(None, "qa-duplicate", "GLOBAL")
    own_id = await skill(tenant, "qa-duplicate", "TENANT")
    ids = (foreign_id,) if selection == "foreign" else (uuid4(),) if selection == "unknown" else (global_id, own_id)
    before = await db.fetchval("SELECT count(*) FROM agents")
    with pytest.raises(HTTPException) as failure:
        await provision(actor, tenant, explicit=ids, servers=("vendor/one",))
    assert failure.value.status_code == 422
    assert failure.value.detail == {
        "code": "agent_setup_skill_folder_conflict"
        if selection == "duplicate_folder"
        else "agent_setup_skill_unavailable"
    }
    assert await db.fetchval("SELECT count(*) FROM agents") == before
    assert await db.fetchval("SELECT count(*) FROM agent_setup_mcp_work") == 0


async def test_http_foreign_explicit_skill_rejects_without_creating_agent(lifecycle):
    from app.api import agents

    db, actor, tenant, foreign, _identity = lifecycle
    foreign_id = await skill(foreign, "qa-private", "PRIVATE FOREIGN")
    principal = await user_dao.get(actor)
    app = FastAPI()
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[agents.get_current_user] = lambda: principal
    before = await db.fetchval("SELECT count(*) FROM agents")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://setup-qa") as client:
        response = await client.post(
            "/api/agents/",
            json={
                "name": "Rejected",
                "tenant_id": str(tenant),
                "skill_ids": [str(foreign_id)],
                "permission_scope_type": "user",
            },
        )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "agent_setup_skill_unavailable"}}
    assert await db.fetchval("SELECT count(*) FROM agents") == before


@pytest.fixture
async def mcp_http(lifecycle):
    _db, actor, _tenant, _foreign, _identity = lifecycle
    principal = await user_dao.get(actor)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_current_user] = lambda: principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://setup-qa") as client:
        yield client


async def finish_required(agent):
    claim = await agent_setup_dao.claim()
    assert claim is not None
    assert claim.agent_id == agent.id
    await agent_setup.run_agent_setup(claim)


async def test_optional_warning_survives_reload_and_exact_retry_never_replays_required_work(
    lifecycle,
    mcp_http,
    monkeypatch,
):
    db, actor, tenant, _foreign, _identity = lifecycle
    counts = Counter()
    recovered = False
    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", AsyncMock(return_value="private-key"))

    async def connect(_key, url, _display):
        counts["connections"] += 1
        return {"namespace": "qa", "connection_id": url.rsplit("/", 1)[-1]}

    async def wire(_transport, request):
        if request.method == "POST":
            server = request.url.path.split("/")[-2]
            counts[server] += 1
            if server == "two" and not recovered:
                return httpx.Response(503, text="private error https://oauth.invalid/?token=secret")
            return httpx.Response(200, json={"result": {"tools": [{"name": "ping", "inputSchema": {}}]}})
        if request.url.path.endswith("/servers"):
            qualified = request.url.params["q"]
            return httpx.Response(200, json={"servers": [{"qualifiedName": qualified, "remote": True}]})
        return httpx.Response(
            200, json={"deploymentUrl": f"https://remote.invalid/{request.url.path.rsplit('/', 1)[-1]}"}
        )

    monkeypatch.setattr(resource_discovery, "_ensure_smithery_connection", connect)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", wire)
    key_write = Mock(wraps=agent_setup.write_gateway_api_key)
    files_write = AsyncMock(wraps=agent_setup.write_missing_agent_files)
    hook = AsyncMock()
    monkeypatch.setattr(agent_setup, "hook_new_agent", hook)
    monkeypatch.setattr(agent_setup, "write_gateway_api_key", key_write)
    monkeypatch.setattr(agent_setup, "write_missing_agent_files", files_write)
    agent = await provision(actor, tenant, servers=("vendor/one", "vendor/two"))
    assert await agent_setup_mcp_dao.claim() is None
    await finish_required(agent)
    for _ in range(2):
        claim = await agent_setup_mcp_dao.claim()
        assert claim is not None
        await agent_setup_mcp_worker.run_agent_setup_mcp(claim)
    durable = await db.fetchall("SELECT server_id,state,warning_code FROM agent_setup_mcp_work ORDER BY server_id")
    assert durable == [
        {"server_id": "vendor/one", "state": "succeeded", "warning_code": None},
        {"server_id": "vendor/two", "state": "warning", "warning_code": "connection_failed"},
    ]
    raw = str(await db.fetchall("SELECT * FROM agent_setup_mcp_work"))
    assert all(secret not in raw for secret in ("private-key", "private error", "oauth.invalid", "oc-lifecycle-secret"))
    recovered = True
    endpoint = f"/api/agents/{agent.id}/setup/mcp-retry"
    responses = await asyncio.gather(*[mcp_http.post(endpoint, json={"server_id": "vendor/two"}) for _ in range(2)])
    assert [response.status_code for response in responses] == [202, 202]
    claim = await agent_setup_mcp_dao.claim()
    assert claim is not None
    assert claim.server_id == "vendor/two"
    await agent_setup_mcp_worker.run_agent_setup_mcp(claim)
    assert counts == {"connections": 2, "one": 1, "two": 2}
    assert key_write.call_count == files_write.await_count == hook.await_count == 1
    assert [(item.state, item.attempts) for item in await agent_setup_mcp_dao.statuses(agent.id)] == [
        ("succeeded", 1),
        ("succeeded", 2),
    ]
    assert (await mcp_http.post(endpoint, json={"server_id": "vendor/one"})).status_code == 409
    assert (await mcp_http.post(endpoint, json={"server_id": "vendor/unknown"})).status_code == 404
