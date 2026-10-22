import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg.conninfo import make_conninfo

from app.api import agents, departments
from app.core import access_cache
from app.core.json_types import str_from_row
from app.core.security import get_current_user
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.records.user import UserRecord
from app.scripts import bootstrap_db
from test_bootstrap_postgres import schema_db as schema_db


@dataclass(frozen=True, slots=True)
class DepartmentStore:
    db: DbConnection
    conninfo: str
    actor: UserRecord
    identity: UUID
    tenant: UUID
    foreign: UUID
    selected: UUID
    other: UUID
    members: tuple[UUID, UUID, UUID]
    agents: tuple[UUID, UUID, UUID]
    invalidated: list[UUID]


@pytest.fixture
async def department_store(schema_db: DbConnection, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DepartmentStore]:
    await bootstrap_db.main()
    row = await schema_db.fetchone("SELECT current_schema() AS schema")
    assert row is not None
    conninfo = make_conninfo(
        os.environ["BOOTSTRAP_TEST_DATABASE_URL"], options=f"-csearch_path={str_from_row(row['schema'])}"
    )
    await init_pool(conninfo=conninfo, min_size=1, max_size=6)
    tenant, foreign, selected, other, actor_id, identity = (uuid4() for _ in range(6))
    members = (uuid4(), uuid4(), uuid4())
    agent_ids = (uuid4(), uuid4(), uuid4())
    await schema_db.execute(
        "INSERT INTO tenants (id, name, slug) VALUES (%(own)s, 'Own', 'own'), (%(foreign)s, 'Other', 'other')",
        {"own": tenant, "foreign": foreign},
    )
    await schema_db.execute(
        "INSERT INTO identities (id, email) VALUES (%(id)s, 'admin@example.test')", {"id": identity}
    )
    await schema_db.execute(
        "INSERT INTO users (id, tenant_id, identity_id, role, display_name) "
        "VALUES (%(id)s, %(tenant)s, %(identity)s, 'org_admin', 'Admin')",
        {"id": actor_id, "tenant": tenant, "identity": identity},
    )
    for member in members:
        await schema_db.execute(
            "INSERT INTO users (id, tenant_id, role, display_name) VALUES (%(id)s, %(tenant)s, 'member', 'Member')",
            {"id": member, "tenant": tenant},
        )
    for department, name in ((selected, "Selected"), (other, "Keep")):
        await schema_db.execute(
            "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, %(name)s)",
            {"id": department, "tenant": tenant, "name": name},
        )
    for member, department in [*((member, selected) for member in members), (members[1], other)]:
        await schema_db.execute(
            "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
            "VALUES (%(user)s, %(tenant)s, %(department)s)",
            {"user": member, "tenant": tenant, "department": department},
        )
    for index, agent in enumerate(agent_ids):
        await schema_db.execute(
            "INSERT INTO agents (id, tenant_id, creator_id, name, access_mode, role_description, "
            "max_triggers, min_poll_interval_min, webhook_rate_limit, company_access_level, heartbeat_interval_minutes) "
            "VALUES (%(id)s, %(tenant)s, %(creator)s, %(name)s, %(mode)s, '', 20, 5, 5, 'use', 240)",
            {
                "id": agent,
                "tenant": tenant,
                "creator": actor_id,
                "name": f"Agent {index}",
                "mode": "company" if index == 2 else "custom",
            },
        )
    grants = [
        (agent_ids[0], "department", selected),
        (agent_ids[0], "department", other),
        (agent_ids[0], "user", members[2]),
        (agent_ids[1], "department", selected),
        (agent_ids[2], "company", None),
    ]
    for agent, scope, scope_id in grants:
        await schema_db.execute(
            "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
            "VALUES (%(id)s, %(agent)s, %(scope)s, %(scope_id)s, 'use')",
            {"id": uuid4(), "agent": agent, "scope": scope, "scope_id": scope_id},
        )
    actor = await user_dao.get_with_identity(actor_id, fresh=True)
    assert actor is not None
    invalidated: list[UUID] = []

    async def invalidate(agent_id: UUID) -> None:
        invalidated.append(agent_id)

    monkeypatch.setattr(access_cache, "_incr_acl_version_now", invalidate)
    monkeypatch.setattr(access_cache, "read_acl_version", AsyncMock(return_value="0"))
    monkeypatch.setattr(access_cache, "get_cached_level", AsyncMock(return_value=None))
    monkeypatch.setattr(access_cache, "set_cached_level", AsyncMock())
    monkeypatch.setattr(agents, "check_agent_creation_quota", AsyncMock())
    monkeypatch.setattr(agents, "assert_models_in_tenant_pool", AsyncMock())
    monkeypatch.setattr(agents, "persist_agent_setup", AsyncMock())
    monkeypatch.setattr(agents, "ensure_access_granted_platform_relationships", AsyncMock(return_value=False))
    try:
        yield DepartmentStore(
            schema_db, conninfo, actor, identity, tenant, foreign, selected, other, members, agent_ids, invalidated
        )
    finally:
        await close_pool()


async def request(store: DepartmentStore, operation: str) -> Response:
    app = FastAPI()
    app.include_router(departments.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: store.actor
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            match operation:
                case "delete":
                    return await client.delete(f"/api/departments/{store.selected}?tenant_id={store.tenant}")
                case "assign":
                    return await client.put(
                        f"/api/users/{store.members[0]}/departments?tenant_id={store.tenant}",
                        json={"department_ids": [str(store.selected), str(store.other)]},
                    )
                case "permission":
                    return await client.put(
                        f"/api/agents/{store.agents[0]}/permissions",
                        json={"scope_type": "custom", "department_ids": [str(store.selected), str(store.other)]},
                    )
                case "create":
                    return await client.post(
                        "/api/agents/",
                        json={
                            "name": "Created",
                            "permission_scope_type": "custom",
                            "department_ids": [str(store.selected)],
                        },
                    )
                case _:
                    raise AssertionError(operation)
    finally:
        app.dependency_overrides.clear()
