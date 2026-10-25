from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import tenants
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.tenant_dao import tenant_dao
from app.db.session import bind_crud_connection, connection_ctx
from app.records.user import UserRecord
from deletion_content_pg import seed_deletion_content

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)


def preview_app(actor: UserRecord) -> FastAPI:
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: actor
    return app


@pytest.mark.asyncio
async def test_preview_requires_authentication() -> None:
    # Given
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api")
    # When
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/tenants/{uuid4()}/deletion-preview")
    # Then
    assert response.status_code == 401


async def seed_preview_ownership(fixture: DeletionPostgres) -> None:
    content = await seed_deletion_content(fixture)
    inactive_user, unassigned_user, extra_agent = (uuid4() for _ in range(3))
    async with connection_ctx() as db:
        for user_id, tenant_id in ((inactive_user, fixture.tenant_id), (unassigned_user, None)):
            await db.execute(
                "INSERT INTO users (id, tenant_id, display_name, is_active) VALUES (%s, %s, 'Inactive', false)",
                (user_id, tenant_id),
            )
        await db.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, "
            "max_triggers, min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
            "max_llm_calls_per_day, heartbeat_interval_minutes) "
            "VALUES (%s, 'Extra', 'Role', %s, %s, 'employee', 1, 5, 5, 'private', 'use', 100, 240)",
            (extra_agent, fixture.other_user_id, fixture.tenant_id),
        )
        await db.execute("UPDATE agents SET creator_id = %s WHERE id = %s", (fixture.actor.id, content.peer_id))
        for agent_id, creator_id, enabled in (
            (fixture.agent_id, fixture.actor.id, True),
            (fixture.agent_id, fixture.other_user_id, False),
            (content.peer_id, fixture.actor.id, False),
            (content.peer_id, inactive_user, True),
            (extra_agent, fixture.other_user_id, True),
            (content.peer_id, fixture.other_user_id, True),
            (content.peer_id, unassigned_user, True),
        ):
            await db.execute(
                "INSERT INTO agent_schedules (id, agent_id, created_by, name, instruction, cron_expr, "
                "is_enabled, run_count) VALUES (%s, %s, %s, 'Schedule', 'Work', '0 * * * *', %s, 0)",
                (uuid4(), agent_id, creator_id, enabled),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("own_tenant", [False, True])
async def test_preview_counts_unique_ownership_union_without_writes_when_authorized(
    deletion_pg: DeletionPostgres, own_tenant: bool
) -> None:
    # Given
    await seed_preview_ownership(deletion_pg)
    before = await deletion_pg.snapshot()
    schedules = await deletion_pg.rows("SELECT * FROM agent_schedules ORDER BY id")
    actor = UserRecord(
        id=deletion_pg.actor.id,
        identity_id=deletion_pg.actor.identity_id,
        tenant_id=deletion_pg.tenant_id if own_tenant else deletion_pg.other_tenant_id,
        role=UserRole.PLATFORM_ADMIN,
    )
    app = preview_app(actor)
    # When
    try:
        async with connection_ctx() as db:
            await db.execute("SET TRANSACTION READ ONLY")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(f"/api/tenants/{deletion_pg.tenant_id}/deletion-preview")
            advisory_locks = await db.fetchval(
                "SELECT count(*) FROM pg_locks WHERE pid = pg_backend_pid() AND locktype = 'advisory'"
            )
    finally:
        app.dependency_overrides.clear()
    # Then
    assert response.status_code == 200
    assert response.json() == {"users_count": 2, "agents_count": 2, "schedules_count": 5}
    assert advisory_locks == 0
    assert await deletion_pg.snapshot() == before
    assert await deletion_pg.rows("SELECT * FROM agent_schedules ORDER BY id") == schedules
    assert deletion_pg.events == []
    assert deletion_pg.runtime_calls == []
    assert (deletion_pg.runtime_root / str(deletion_pg.agent_id) / "SOUL.md").exists()


@pytest.mark.asyncio
async def test_preview_returns_zero_counts_for_empty_company(deletion_pg: DeletionPostgres) -> None:
    # Given
    empty_tenant = uuid4()
    async with connection_ctx() as db:
        await db.execute("INSERT INTO tenants (id, name, slug) VALUES (%s, 'Empty', 'empty')", (empty_tenant,))
    actor = UserRecord(id=uuid4(), identity_id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    app = preview_app(actor)
    # When
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/tenants/{empty_tenant}/deletion-preview")
    finally:
        app.dependency_overrides.clear()
    # Then
    assert response.status_code == 200
    assert response.json() == {"users_count": 0, "agents_count": 0, "schedules_count": 0}


@pytest.mark.asyncio
async def test_preview_reads_current_counts_after_records_change(deletion_pg: DeletionPostgres) -> None:
    # Given
    assert await tenant_dao.deletion_preview(deletion_pg.tenant_id) == {
        "users_count": 1,
        "agents_count": 1,
        "schedules_count": 0,
    }
    await seed_preview_ownership(deletion_pg)
    # When
    result = await tenant_dao.deletion_preview(deletion_pg.tenant_id)
    # Then
    assert result == {"users_count": 2, "agents_count": 2, "schedules_count": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["own", "genesis", "foreign", "member", "system", "default", "missing"])
async def test_preview_http_rejects_ineligible_company(deletion_pg: DeletionPostgres, condition: str) -> None:
    # Given
    target_id: UUID = uuid4() if condition == "missing" else deletion_pg.tenant_id
    actor = UserRecord(
        id=deletion_pg.actor.id,
        identity_id=deletion_pg.actor.identity_id,
        tenant_id=deletion_pg.other_tenant_id if condition == "foreign" else deletion_pg.tenant_id,
        role=UserRole.ORG_ADMIN
        if condition in ("own", "genesis", "foreign")
        else (UserRole.MEMBER if condition == "member" else UserRole.PLATFORM_ADMIN),
        is_genesis=condition == "genesis",
    )
    if condition in ("system", "default"):
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE tenants SET is_system = %s, is_default_end_user_org = %s WHERE id = %s",
                (condition == "system", condition == "default", target_id),
            )
    before = await deletion_pg.snapshot()
    app = preview_app(actor)
    # When
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/tenants/{target_id}/deletion-preview")
    finally:
        app.dependency_overrides.clear()
    # Then
    expected_status = (
        404 if condition == "missing" else 403 if condition in ("own", "genesis", "foreign", "member") else 400
    )
    assert response.status_code == expected_status
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.events == []
    assert deletion_pg.runtime_calls == []
