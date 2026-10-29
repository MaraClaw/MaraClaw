from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from app.api import agents, departments
from app.core.security import get_current_user
from app.db.connection import Row
from app.db.session import connection_ctx
from app.schemas.department import DepartmentMergePreviewOut
from department_delete_postgres_fixtures import DepartmentStore, department_store, request
from test_bootstrap_postgres import schema_db

__all__ = ["department_store", "schema_db"]


@pytest.fixture
async def merge_store(department_store: DepartmentStore) -> tuple[DepartmentStore, UUID, UUID]:
    store = department_store
    extra_agent, unrelated_department = uuid4(), uuid4()
    await store.db.execute(
        "INSERT INTO agents (id, tenant_id, creator_id, name, access_mode, role_description, "
        "max_triggers, min_poll_interval_min, webhook_rate_limit, company_access_level, heartbeat_interval_minutes) "
        "SELECT %(id)s, tenant_id, creator_id, 'Destination only', 'custom', role_description, "
        "max_triggers, min_poll_interval_min, webhook_rate_limit, company_access_level, heartbeat_interval_minutes "
        "FROM agents WHERE id = %(template)s",
        {"id": extra_agent, "template": store.agents[0]},
    )
    for agent, department in ((extra_agent, store.other), (store.agents[2], store.selected)):
        await store.db.execute(
            "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
            "VALUES (%(id)s, %(agent)s, 'department', %(department)s, 'use')",
            {"id": uuid4(), "agent": agent, "department": department},
        )
    await store.db.execute(
        "DELETE FROM local_department_memberships WHERE user_id = %(user)s AND department_id = %(source)s",
        {"user": store.members[2], "source": store.selected},
    )
    await store.db.execute(
        "INSERT INTO local_departments (id, tenant_id, name, description) VALUES (%(id)s, %(tenant)s, 'Unrelated', '')",
        {"id": unrelated_department, "tenant": store.tenant},
    )
    for user, department in ((store.members[2], store.other), (store.members[0], unrelated_department)):
        await store.db.execute(
            "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
            "VALUES (%(user)s, %(tenant)s, %(department)s)",
            {"user": user, "tenant": store.tenant, "department": department},
        )
    return store, extra_agent, unrelated_department


@asynccontextmanager
async def merge_client(store: DepartmentStore) -> AsyncGenerator[AsyncClient]:
    app = FastAPI()
    app.include_router(departments.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: store.actor
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def merge_request(store: DepartmentStore, operation: str, *, confirmation_token: str | None = None) -> Response:
    async with merge_client(store) as client:
        match operation:
            case "membership_swap":
                first = await client.put(
                    f"/api/users/{store.members[0]}/departments?tenant_id={store.tenant}",
                    json={"department_ids": [str(store.other)]},
                )
                if first.status_code != 200:
                    return first
                return await client.put(
                    f"/api/users/{store.members[2]}/departments?tenant_id={store.tenant}",
                    json={"department_ids": [str(store.selected)]},
                )
            case "grant_swap":
                async with connection_ctx() as db:
                    row = await db.fetchone(
                        "SELECT a.id FROM agents a WHERE a.name = 'Destination only' AND a.tenant_id = %(tenant)s",
                        {"tenant": store.tenant},
                    )
                assert row is not None
                first = await client.put(
                    f"/api/agents/{store.agents[1]}/permissions",
                    json={"scope_type": "custom", "department_ids": [str(store.other)]},
                )
                if first.status_code != 200:
                    return first
                return await client.put(
                    f"/api/agents/{row['id']}/permissions",
                    json={"scope_type": "custom", "department_ids": [str(store.selected)]},
                )
            case "merge" | "reverse_merge":
                source, destination = (
                    (store.selected, store.other) if operation == "merge" else (store.other, store.selected)
                )
                if confirmation_token is None:
                    reviewed = await client.get(
                        f"/api/departments/{source}/merge-preview?tenant_id={store.tenant}&destination_id={destination}"
                    )
                    assert reviewed.status_code == 200, reviewed.text
                    confirmation_token = DepartmentMergePreviewOut.model_validate_json(
                        reviewed.content
                    ).confirmation_token
                return await client.post(
                    f"/api/departments/{source}/merge?tenant_id={store.tenant}",
                    json={"destination_id": str(destination), "confirmation_token": confirmation_token},
                )
            case "preview":
                return await client.get(
                    f"/api/departments/{store.selected}/merge-preview?"
                    f"tenant_id={store.tenant}&destination_id={store.other}"
                )
            case "rename" | "destination_rename":
                selected = store.selected if operation == "rename" else store.other
                return await client.patch(f"/api/departments/{selected}", json={"name": "Renamed"})
            case "destination_delete":
                return await client.delete(f"/api/departments/{store.other}")
            case _:
                return await request(store, operation)


async def merge_state(store: DepartmentStore) -> tuple[list[Row], list[Row], list[Row], list[Row]]:
    return (
        await store.db.fetchall("SELECT id, name, description FROM local_departments ORDER BY id"),
        await store.db.fetchall(
            "SELECT user_id, department_id FROM local_department_memberships ORDER BY user_id, department_id"
        ),
        await store.db.fetchall(
            "SELECT id, agent_id, scope_type, scope_id, access_level FROM agent_permissions ORDER BY id"
        ),
        await store.db.fetchall("SELECT id FROM admin_audit_logs ORDER BY id"),
    )
