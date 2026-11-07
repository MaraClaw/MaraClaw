from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg.conninfo import make_conninfo
from psycopg.errors import QueryCanceled

from app.api import background_tasks, tenants
from app.config import get_settings
from app.core.redis_cache import cache_key
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.db import session
from app.db.errors import DbError, ForeignKeyViolationError
from app.db.pool import EnginePool, close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.services.admin_audit import write_admin_audit
from deletion_content_pg import seed_deletion_content

if TYPE_CHECKING:
    from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)


@pytest.mark.asyncio
async def test_delete_removes_owned_rows_and_retains_audit_when_http_request_commits(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given
    content = await seed_deletion_content(deletion_pg)
    app = FastAPI()
    app.include_router(tenants.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: replace(deletion_pg.actor, role="platform_admin")
    # When
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(f"/api/tenants/{deletion_pg.tenant_id}")
    finally:
        app.dependency_overrides.clear()
    # Then
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "fallback_tenant_id": None}
    rows = await deletion_pg.snapshot()
    assert [row["id"] for row in rows["tenants"]] == [deletion_pg.other_tenant_id]
    assert [row["id"] for row in rows["users"]] == [deletion_pg.other_user_id]
    assert [row["id"] for row in rows["agents"]] == [content.peer_id]
    assert {row["name"] for row in rows["skills"]} == {"Unrelated", "Global"}
    assert {row["content"] for row in rows["skill_files"]} == {"Unrelated", "Global"}
    assert len(rows["admin_audit_logs"]) == 1
    audit = rows["admin_audit_logs"][0]
    assert audit["actor_id"] is None
    assert audit["tenant_id"] is None
    assert audit["target_id"] == deletion_pg.tenant_id
    assert audit["actor_email"] == "owner@example.test"
    assert audit["actor_role"] == "platform_admin"
    assert audit["action"] == "tenant_delete"
    assert audit["changes"] == {"deleted": {"before": False, "after": True}}
    assert audit["details"] == {"tenant_name": "Target"}
    assert {row["id"] for row in rows["clawbook_posts"]} == {content.surviving_post_id, content.untouched_post_id}
    assert {row["ref_id"] for row in rows["participants"]} == {content.peer_id, deletion_pg.other_user_id}
    for table in ("clawbook_comments", "clawbook_likes"):
        assert len(rows[table]) == 1
        assert rows[table][0]["author_id"] == deletion_pg.other_user_id
    assert await deletion_pg.rows(
        "SELECT likes_count, comments_count FROM clawbook_posts WHERE id = %s",
        (content.surviving_post_id,),
    ) == [{"likes_count": 1, "comments_count": 1}]
    assert await deletion_pg.rows(
        "SELECT likes_count, comments_count FROM clawbook_posts WHERE id = %s",
        (content.untouched_post_id,),
    ) == [{"likes_count": 0, "comments_count": 0}]
    assert len(rows["sso_scan_sessions"]) == 1
    assert rows["sso_scan_sessions"][0]["user_id"] == deletion_pg.other_user_id
    assert len(rows["chat_messages"]) == len(rows["chat_sessions"]) == 1
    assert len(rows["web_search_events"]) == 1
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert deletion_pg.runtime_calls == [("remove", deletion_pg.agent_id), ("archive", deletion_pg.agent_id)]
    history = deletion_pg.runtime_root / "archive" / str(deletion_pg.agent_id) / "task_history.json"
    assert json.loads(history.read_text())["tasks"][0]["logs"][0]["content"] == "Completed"


@pytest.mark.asyncio
async def test_delete_clears_only_departing_author_links_and_tombstones_orphans(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given
    before = await deletion_pg.snapshot()
    # When
    await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
    # Then
    after = await deletion_pg.snapshot()
    for table, author in (("agent_templates", "created_by"), ("enterprise_info", "updated_by")):
        assert after[table] == [
            {**row, author: None} if row[author] == deletion_pg.actor.id else row for row in before[table]
        ]
    orphan = next(row for row in after["identities"] if row["id"] == deletion_pg.actor.identity_id)
    assert {key: orphan[key] for key in ("email", "phone", "password_hash")} == {
        "email": None,
        "phone": None,
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
    result = await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
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
    await seed_deletion_content(deletion_pg)
    before = await deletion_pg.snapshot()

    async def failed_fallback(_identity_id: object, *, exclude_tenant_id: object) -> None:
        raise RuntimeError("later failure")

    monkeypatch.setattr(tenants.user_dao, "fallback_tenant_for_identity", failed_fallback)
    # When
    with pytest.raises(RuntimeError, match="later failure"):
        await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
    # Then
    assert await deletion_pg.snapshot() == before
    assert deletion_pg.events == []
    assert deletion_pg.runtime_calls == []
    assert (deletion_pg.runtime_root / str(deletion_pg.agent_id) / "SOUL.md").exists()
    assert not (deletion_pg.runtime_root / "archive").exists()


@pytest.mark.asyncio
async def test_delete_rolls_back_all_changes_when_unexpected_fk_blocks_tenant(deletion_pg: DeletionPostgres) -> None:
    # Given
    async with connection_ctx() as db:
        await db.execute("CREATE TABLE deletion_blocker (tenant_id UUID REFERENCES tenants(id))")
        await db.execute("INSERT INTO deletion_blocker VALUES (%s)", (deletion_pg.tenant_id,))
    before = await deletion_pg.snapshot()
    # When
    with pytest.raises(ForeignKeyViolationError):
        await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
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
    result = await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
    # Then
    assert await deletion_pg.rows("SELECT * FROM identities WHERE id = %s", (deletion_pg.actor.identity_id,)) == before
    assert result["fallback_tenant_id"] == (str(remaining_tenant) if remaining_tenant else None)


@pytest.mark.asyncio
async def test_delete_defers_snapshots_versions_and_acl_until_outer_request_commit(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given
    await seed_deletion_content(deletion_pg)
    assert deletion_pg.events == []
    # When
    async with connection_ctx():
        await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
        assert deletion_pg.events == []
        assert deletion_pg.runtime_calls == []
        assert (deletion_pg.runtime_root / str(deletion_pg.agent_id) / "SOUL.md").exists()
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
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert deletion_pg.runtime_calls == [("remove", deletion_pg.agent_id), ("archive", deletion_pg.agent_id)]


@pytest_asyncio.fixture
async def low_limit_pool(deletion_pg: DeletionPostgres, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[EnginePool]:
    monkeypatch.setenv("DATABASE_STATEMENT_TIMEOUT_MS", "200")
    get_settings.cache_clear()
    [row] = await deletion_pg.rows("SELECT current_schema() AS schema")
    conninfo = make_conninfo(os.environ["MARACLAW_TEST_POSTGRES_DSN"], options=f"-csearch_path={row['schema']}")
    pool = await init_pool(conninfo=conninfo, min_size=1, max_size=3)
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    try:
        yield pool
    finally:
        try:
            await asyncio.gather(*tuple(background_tasks._tasks))
        finally:
            await close_pool()
            get_settings.cache_clear()


_SLOW_DELETE_TRIGGER = (
    "CREATE TABLE slow_delete_hits (hit integer NOT NULL)",
    (
        "CREATE FUNCTION slow_delete() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN PERFORM pg_sleep(1); INSERT INTO slow_delete_hits VALUES (1); RETURN OLD; END $$"
    ),
    "CREATE TRIGGER slow_delete BEFORE DELETE ON agents FOR EACH ROW EXECUTE FUNCTION slow_delete()",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", ["tenant", "agent"])
async def test_bulk_delete_outlasts_statement_limit_and_restores_it_inside_the_request(
    deletion_pg: DeletionPostgres, low_limit_pool: EnginePool, bulk: str
) -> None:
    # Given a 200 ms statement limit and a trigger making each agent delete take 1 s, which the limit cancels.
    async with deletion_pg.pool.connection() as raw:
        for statement in _SLOW_DELETE_TRIGGER:
            await raw.execute(statement)
    with pytest.raises(QueryCanceled):
        async with low_limit_pool.connection() as raw:
            await raw.execute("DELETE FROM agents WHERE id = %s", (deletion_pg.agent_id,))
    assert await deletion_pg.rows("SELECT hit FROM slow_delete_hits") == []
    # When the bulk delete runs inside an outer request transaction.
    async with connection_ctx() as db:
        before = await db.fetchval("SHOW statement_timeout")
        match bulk:
            case "tenant":
                await tenants.delete_tenant(deletion_pg.tenant_id, replace(deletion_pg.actor, role="platform_admin"))
            case "agent":
                await agent_dao.delete_with_related(deletion_pg.agent_id)
            case _:
                pytest.fail(f"Unknown bulk delete: {bulk}")
        after = await db.fetchval("SHOW statement_timeout")
        with pytest.raises(DbError) as cancelled:
            async with db.raw.transaction():
                await db.execute("SELECT pg_sleep(1)")
    # Then the slow delete finished, the limit applied before and after it, and later statements are still cancelled.
    assert (before, after) == ("200ms", "200ms")
    assert isinstance(cancelled.value.orig, QueryCanceled)
    assert await deletion_pg.rows("SELECT hit FROM slow_delete_hits") == [{"hit": 1}]
    assert await deletion_pg.rows("SELECT id FROM agents") == []
    remaining = {row["id"] for row in await deletion_pg.rows("SELECT id FROM tenants")}
    assert remaining == {deletion_pg.other_tenant_id} | ({deletion_pg.tenant_id} if bulk == "agent" else set())
