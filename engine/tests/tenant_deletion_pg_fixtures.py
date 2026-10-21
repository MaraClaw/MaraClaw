"""Isolated baseline schemas; set MARACLAW_TEST_POSTGRES_DSN to run live regressions."""

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.api import background_tasks
from app.core import access_cache, redis_cache, row_memo, session_cache, tenant_cache
from app.db import session
from app.db.connection import DbConnection, Params, Row
from app.records.agent import AgentRecord
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.services.agent_manager import agent_manager

BASELINE_SQL = (Path(__file__).resolve().parents[1] / "scripts" / "schema_baseline.sql").read_bytes()


@dataclass(frozen=True, slots=True)
class DeletionPostgres:
    pool: AsyncConnectionPool[AsyncConnection[DictRow]]
    tenant_id: UUID
    other_tenant_id: UUID
    actor: UserRecord
    other_user_id: UUID
    agent_id: UUID
    events: list[str]
    runtime_calls: list[tuple[str, UUID]]
    runtime_root: Path

    async def rows(self, query: str, params: Params = None) -> list[Row]:
        async with self.pool.connection() as raw:
            return await DbConnection(raw).fetchall(query, params)

    async def snapshot(self) -> dict[str, list[Row]]:
        tables = (
            "tenants",
            "users",
            "identities",
            "agents",
            "skills",
            "skill_files",
            "agent_templates",
            "enterprise_info",
            "admin_audit_logs",
            "participants",
            "plaza_posts",
            "plaza_comments",
            "plaza_likes",
            "sso_scan_sessions",
            "chat_messages",
            "chat_sessions",
            "tasks",
            "task_logs",
            "web_search_events",
        )
        result: dict[str, list[Row]] = {}
        async with self.pool.connection() as raw:
            for table in tables:
                cursor = await raw.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(table)))
                result[table] = await cursor.fetchall()
        return result


@pytest_asyncio.fixture
async def deletion_pg(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[DeletionPostgres]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("Set MARACLAW_TEST_POSTGRES_DSN to an isolated PostgreSQL database")
    schema = "tenant_delete_" + uuid4().hex
    async with await AsyncConnection.connect(dsn, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            async with AsyncConnectionPool[AsyncConnection[DictRow]](
                dsn,
                min_size=1,
                max_size=3,
                open=False,
                kwargs={"row_factory": dict_row, "options": f"-c search_path={schema}"},
            ) as pool:
                await pool.wait()
                async with pool.connection() as raw:
                    await raw.execute(BASELINE_SQL, prepare=False)
                monkeypatch.setattr(session, "get_pool", lambda: pool)
                monkeypatch.setattr(session_cache, "_ttl", lambda: 60)
                monkeypatch.setattr(tenant_cache, "_ttl", lambda: 60)
                events: list[str] = []
                tenant_id, other_tenant_id, identity_id, other_identity_id = (uuid4() for _ in range(4))
                user_id, other_user_id, agent_id = (uuid4() for _ in range(3))
                fixture = DeletionPostgres(
                    pool,
                    tenant_id,
                    other_tenant_id,
                    UserRecord(
                        id=user_id,
                        identity_id=identity_id,
                        tenant_id=tenant_id,
                        role="org_admin",
                        identity=IdentityRecord(id=identity_id, email="owner@example.test"),
                    ),
                    other_user_id,
                    agent_id,
                    events,
                    [],
                    tmp_path,
                )

                async def remove_container(agent: AgentRecord) -> bool:
                    assert await fixture.rows("SELECT id FROM agents WHERE id = %s", (agent.id,)) == []
                    fixture.runtime_calls.append(("remove", agent.id))
                    return True

                async def archive_files(agent_id: UUID) -> Path:
                    fixture.runtime_calls.append(("archive", agent_id))
                    destination = tmp_path / "archive" / str(agent_id)
                    destination.parent.mkdir(exist_ok=True)
                    source = tmp_path / str(agent_id)
                    if source.exists():
                        shutil.move(source, destination)
                    else:
                        destination.mkdir()
                    return destination

                monkeypatch.setattr(agent_manager, "remove_container", remove_container)
                monkeypatch.setattr(agent_manager, "archive_agent_files", archive_files)

                async def version_now(key: str, *, ttl: int | None = None) -> None:
                    remaining = await fixture.rows("SELECT id FROM tenants WHERE id = %s", (tenant_id,))
                    assert remaining == [], "Version invalidation ran before deletion committed"
                    events.append(key)

                async def acl_now(deleted_agent_id: UUID) -> None:
                    remaining = await fixture.rows("SELECT id FROM agents WHERE id = %s", (deleted_agent_id,))
                    assert remaining == [], "ACL invalidation ran before deletion committed"
                    events.append(f"aclver:{deleted_agent_id}")

                async def cache_get(_key: str) -> None:
                    return None

                class DeleteObserver:
                    async def delete(self, *keys: str) -> None:
                        events.extend(f"delete:{key}" for key in keys)
                        remaining = await fixture.rows("SELECT id FROM tenants WHERE id = %s", (tenant_id,))
                        if remaining:
                            events.append("delete_before_commit")
                        assert remaining == [], "Snapshot deletion ran before deletion committed"

                async def cache_client() -> DeleteObserver:
                    return DeleteObserver()

                if getattr(request, "param", None) != "warm":
                    monkeypatch.setattr(redis_cache, "_incr_version_now", version_now)
                    monkeypatch.setattr(access_cache, "_drop_acl_version_now", acl_now)
                    monkeypatch.setattr(redis_cache, "cache_get", cache_get)
                    monkeypatch.setattr(redis_cache, "_client", cache_client)
                async with pool.connection() as raw:
                    db = DbConnection(raw)
                    for tid, uid, iid, name in (
                        (tenant_id, user_id, identity_id, "Target"),
                        (other_tenant_id, other_user_id, other_identity_id, "Unrelated"),
                    ):
                        await db.execute(
                            "INSERT INTO tenants (id, name, slug) VALUES (%s, %s, %s)", (tid, name, name.lower())
                        )
                        await db.execute(
                            "INSERT INTO identities (id, email, phone, password_hash) VALUES (%s, %s, %s, 'hash')",
                            (
                                iid,
                                "owner@example.test" if iid == identity_id else "other@example.test",
                                name.lower(),
                            ),
                        )
                        await db.execute(
                            "INSERT INTO users (id, identity_id, tenant_id, display_name, role) "
                            "VALUES (%s, %s, %s, %s, 'org_admin')",
                            (uid, iid, tid, name),
                        )
                        await db.execute(
                            "INSERT INTO agent_templates (id, name, description, icon, category, soul_template, "
                            "default_skills, default_mcp_servers, capability_bullets, is_builtin, created_by) "
                            "VALUES (%s, %s, 'description', 'icon', 'custom', 'soul', '[]', '[]', '[]', false, %s)",
                            (uuid4(), name, uid),
                        )
                        await db.execute(
                            "INSERT INTO enterprise_info (id, info_type, content, version, visible_roles, updated_by) "
                            "VALUES (%s, %s, '{}', 1, '[]', %s)",
                            (uuid4(), name, uid),
                        )
                    for tid, name in ((tenant_id, "Target"), (other_tenant_id, "Unrelated"), (None, "Global")):
                        skill_id = uuid4()
                        await db.execute(
                            "INSERT INTO skills (id, tenant_id, name, description, category, icon, folder_name, "
                            "is_builtin, is_default) VALUES (%s, %s, %s, 'description', 'custom', 's', %s, false, false)",
                            (skill_id, tid, name, name.lower()),
                        )
                        await db.execute(
                            "INSERT INTO skill_files (id, skill_id, path, content) VALUES (%s, %s, %s, %s)",
                            (uuid4(), skill_id, "SKILL.md", name),
                        )
                    await db.execute(
                        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, "
                        "max_triggers, min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
                        "max_llm_calls_per_day, heartbeat_interval_minutes) "
                        "VALUES (%s, 'Agent', 'Role', %s, %s, 'employee', 1, 5, 5, 'private', 'use', 100, 240)",
                        (agent_id, user_id, tenant_id),
                    )
                row_memo.clear_row_memo()
                try:
                    yield fixture
                finally:
                    await asyncio.gather(*tuple(background_tasks._tasks))
                    row_memo.clear_row_memo()
        finally:
            await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
