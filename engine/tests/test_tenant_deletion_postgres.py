from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import tenants
from app.core.redis_cache import cache_key
from app.core.security import get_current_user
from app.db.errors import ForeignKeyViolationError
from app.db.session import bind_crud_connection, connection_ctx
from app.services.admin_audit import write_admin_audit
from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)


@pytest.mark.asyncio
async def test_delete_removes_owned_rows_and_retains_audit_when_http_request_commits(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: deletion_pg.actor
    # When
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(f"/api/tenants/{deletion_pg.tenant_id}")
    # Then
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "fallback_tenant_id": None}
    rows = await deletion_pg.snapshot()
    assert [row["id"] for row in rows["tenants"]] == [deletion_pg.other_tenant_id]
    assert [row["id"] for row in rows["users"]] == [deletion_pg.other_user_id]
    assert rows["agents"] == []
    assert {row["name"] for row in rows["skills"]} == {"Unrelated", "Global"}
    assert {row["content"] for row in rows["skill_files"]} == {"Unrelated", "Global"}
    assert len(rows["admin_audit_logs"]) == 1
    audit = rows["admin_audit_logs"][0]
    assert audit["actor_id"] is None
    assert audit["tenant_id"] is None
    assert audit["target_id"] == deletion_pg.tenant_id
    assert audit["actor_email"] == "owner@example.test"
    assert audit["actor_role"] == "org_admin"
    assert audit["action"] == "tenant_delete"
    assert audit["changes"] == {"deleted": {"before": False, "after": True}}
    assert audit["details"] == {"tenant_name": "Target"}


@pytest.mark.asyncio
async def test_delete_clears_only_departing_author_links_and_tombstones_orphans(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given
    before = await deletion_pg.snapshot()
    # When
    await tenants.delete_tenant(deletion_pg.tenant_id, deletion_pg.actor)
    # Then
    after = await deletion_pg.snapshot()
    for table, author in (("agent_templates", "created_by"), ("enterprise_info", "updated_by")):
        assert after[table] == [
            {**row, author: None} if row[author] == deletion_pg.actor.id else row for row in before[table]
        ]
    orphan = next(row for row in after["identities"] if row["id"] == deletion_pg.actor.identity_id)
    assert {key: orphan[key] for key in ("email", "phone", "username", "password_hash")} == {
        "email": None,
        "phone": None,
        "username": None,
        "password_hash": None,
    }
    assert orphan["is_active"] is False
    assert orphan["is_platform_admin"] is False
    other_before = [row for row in before["identities"] if row["id"] != deletion_pg.actor.identity_id]
    assert [row for row in after["identities"] if row["id"] != deletion_pg.actor.identity_id] == other_before


@pytest.mark.asyncio
async def test_delete_succeeds_when_real_audit_insert_violates_sql_constraint(deletion_pg: DeletionPostgres) -> None:
    # Given
    async with connection_ctx() as db:
        await db.execute("ALTER TABLE admin_audit_logs ADD CONSTRAINT reject_delete CHECK (action <> 'tenant_delete')")
    # When
    result = await tenants.delete_tenant(deletion_pg.tenant_id, deletion_pg.actor)
    # Then
    assert result == {"status": "deleted", "fallback_tenant_id": None}
    assert await deletion_pg.rows("SELECT id FROM tenants WHERE id = %s", (deletion_pg.tenant_id,)) == []
    assert await deletion_pg.rows("SELECT id FROM admin_audit_logs") == []
    assert deletion_pg.events


@pytest.mark.asyncio
async def test_audit_savepoint_does_not_commit_idle_outer_transaction(deletion_pg: DeletionPostgres) -> None:
    # Given
    before = await deletion_pg.snapshot()

    async def failed_request() -> None:
        async with connection_ctx():
            await write_admin_audit(actor=deletion_pg.actor, action="probe", target_type="tenant")
            raise RuntimeError("later failure")

    # When
    with pytest.raises(RuntimeError, match="later failure"):
        await failed_request()
    # Then
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.events == []


@pytest.mark.asyncio
async def test_delete_rolls_back_audit_rows_and_invalidation_when_later_fallback_fails(
    deletion_pg: DeletionPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    before = await deletion_pg.snapshot()

    async def failed_fallback(_identity_id: object, *, exclude_tenant_id: object) -> None:
        raise RuntimeError("later failure")

    monkeypatch.setattr(tenants.user_dao, "fallback_tenant_for_identity", failed_fallback)
    # When
    with pytest.raises(RuntimeError, match="later failure"):
        await tenants.delete_tenant(deletion_pg.tenant_id, deletion_pg.actor)
    # Then
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.events == []


@pytest.mark.asyncio
async def test_delete_rolls_back_all_changes_when_unexpected_fk_blocks_tenant(deletion_pg: DeletionPostgres) -> None:
    # Given
    async with connection_ctx() as db:
        await db.execute("CREATE TABLE deletion_blocker (tenant_id UUID REFERENCES tenants(id))")
        await db.execute("INSERT INTO deletion_blocker VALUES (%s)", (deletion_pg.tenant_id,))
    before = await deletion_pg.snapshot()
    # When
    with pytest.raises(ForeignKeyViolationError):
        await tenants.delete_tenant(deletion_pg.tenant_id, deletion_pg.actor)
    # Then
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.events == []
    assert await deletion_pg.rows("SELECT tenant_id FROM deletion_blocker") == [{"tenant_id": deletion_pg.tenant_id}]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_other_tenant", [False, True])
async def test_delete_retains_identity_with_inactive_remaining_membership(
    deletion_pg: DeletionPostgres,
    legacy_other_tenant: bool,
) -> None:
    # Given
    remaining_tenant = deletion_pg.other_tenant_id if legacy_other_tenant else None
    async with connection_ctx() as db:
        if legacy_other_tenant:
            # Baseline supports legacy multi-membership DBs by skipping this index when duplicates exist.
            await db.execute("DROP INDEX ux_users_identity_single_tenant")
        await db.execute(
            "INSERT INTO users (id, identity_id, tenant_id, display_name, is_active) VALUES (%s, %s, %s, 'Retained', false)",
            (uuid4(), deletion_pg.actor.identity_id, remaining_tenant),
        )
    before = await deletion_pg.rows("SELECT * FROM identities WHERE id = %s", (deletion_pg.actor.identity_id,))
    # When
    result = await tenants.delete_tenant(deletion_pg.tenant_id, deletion_pg.actor)
    # Then
    assert await deletion_pg.rows("SELECT * FROM identities WHERE id = %s", (deletion_pg.actor.identity_id,)) == before
    assert result["fallback_tenant_id"] == (str(remaining_tenant) if remaining_tenant else None)


@pytest.mark.asyncio
async def test_delete_defers_snapshots_versions_and_acl_until_outer_request_commit(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given
    assert deletion_pg.events == []
    # When
    async with connection_ctx():
        await tenants.delete_tenant(deletion_pg.tenant_id, deletion_pg.actor)
        assert deletion_pg.events == []
        assert await deletion_pg.rows("SELECT id FROM tenants WHERE id = %s", (deletion_pg.tenant_id,)) == [
            {"id": deletion_pg.tenant_id}
        ]
    # Then
    assert set(deletion_pg.events) == {
        f"aclver:{deletion_pg.agent_id}",
        cache_key("sessver", "u", deletion_pg.actor.id),
        cache_key("sessver", "i", deletion_pg.actor.identity_id),
        cache_key("tenantver", deletion_pg.tenant_id),
        "delete:" + cache_key("sess", "v1", deletion_pg.actor.id),
        "delete:" + cache_key("tenant", "v1", deletion_pg.tenant_id),
    }
    assert len(deletion_pg.events) == 6
