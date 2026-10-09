"""Baseline PostgreSQL regressions for administrative tenant deletion.

These tests apply ``scripts/schema_baseline.sql`` and call ``delete_tenant``.
Set ``MARACLAW_TEST_DATABASE_URL`` to a maintenance database the runner may use
to create and drop ``maraclaw_tenant_delete_pg``. The suite skips without it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Json

from app.api import tenants as tenants_api
from app.core import access_cache, redis_cache, session_cache, tenant_cache
from app.db.errors import ForeignKeyViolationError
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx
from app.db.url import normalize_psycopg_conninfo
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies

_DATABASE_NAME = "maraclaw_tenant_delete_pg"
_SKIP_REASON = "Set MARACLAW_TEST_DATABASE_URL to run baseline PostgreSQL tenant-deletion tests"


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


async def _prepare_database(admin_url: str) -> str:
    admin_url = normalize_psycopg_conninfo(_with_database(admin_url, "postgres"))
    target = normalize_psycopg_conninfo(_with_database(admin_url, _DATABASE_NAME))
    connection = await psycopg.AsyncConnection.connect(admin_url, autocommit=True)
    try:
        await connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(_DATABASE_NAME)))
        await connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_DATABASE_NAME)))
    finally:
        await connection.close()

    schema = SCHEMA_BASELINE.read_text(encoding="utf-8")
    connection = await psycopg.AsyncConnection.connect(target)
    try:
        for body in _statement_bodies(schema):
            try:
                await connection.execute(body)
            except Exception as exc:
                raise RuntimeError(f"baseline statement failed: {body[:180]}") from exc
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise
    finally:
        await connection.close()
    return target


async def _drop_database(admin_url: str) -> None:
    admin_url = normalize_psycopg_conninfo(_with_database(admin_url, "postgres"))
    connection = await psycopg.AsyncConnection.connect(admin_url, autocommit=True)
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (_DATABASE_NAME,),
        )
        await connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(_DATABASE_NAME)))
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def tenant_delete_database() -> Iterator[str]:
    raw = os.environ.get("MARACLAW_TEST_DATABASE_URL", "").strip()
    if not raw:
        pytest.skip(_SKIP_REASON)
    url = asyncio.run(_prepare_database(raw))
    try:
        yield url
    finally:
        asyncio.run(_drop_database(raw))


@pytest.fixture
async def database(tenant_delete_database: str) -> AsyncIterator[None]:
    await init_pool(conninfo=tenant_delete_database, min_size=1, max_size=2)
    try:
        await _truncate()
        yield
    finally:
        await close_pool()


def _actor(*, user_id: UUID, tenant_id: UUID, identity_id: UUID, role: str, email: str) -> UserRecord:
    return UserRecord(
        id=user_id,
        identity_id=identity_id,
        tenant_id=tenant_id,
        display_name="Deletion actor",
        role=role,
        identity=IdentityRecord(id=identity_id, email=email, username="actor", is_active=True),
    )


async def _exec(statement: str, params: dict[str, object] | None = None) -> None:
    async with connection_ctx() as db:
        await db.execute(statement, params)


async def _one(statement: str, params: dict[str, object] | None = None) -> dict[str, object] | None:
    async with connection_ctx() as db:
        return await db.fetchone(statement, params)


async def _val(statement: str, params: dict[str, object] | None = None) -> object:
    async with connection_ctx() as db:
        return await db.fetchval(statement, params)


async def _truncate() -> None:
    await _exec(
        "TRUNCATE TABLE admin_audit_logs, skill_files, skills, agents, "
        "local_department_memberships, local_departments, invitation_codes, "
        "agent_templates, enterprise_info, users, identities, tenants "
        "RESTART IDENTITY CASCADE"
    )


async def _insert_tenant(tenant_id: UUID, name: str) -> None:
    await _exec(
        "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, %(name)s, %(slug)s)",
        {"id": tenant_id, "name": name, "slug": f"{name[:12].lower()}-{tenant_id.hex[:8]}"},
    )


async def _insert_identity(identity_id: UUID, email: str, username: str, phone: str) -> None:
    await _exec(
        "INSERT INTO identities (id, email, phone, username, password_hash, is_active) "
        "VALUES (%(id)s, %(email)s, %(phone)s, %(username)s, 'hash', true)",
        {"id": identity_id, "email": email, "phone": phone, "username": username},
    )


async def _insert_user(user_id: UUID, identity_id: UUID, tenant_id: UUID, role: str, name: str) -> None:
    await _exec(
        "INSERT INTO users (id, identity_id, tenant_id, display_name, role) "
        "VALUES (%(id)s, %(identity_id)s, %(tenant_id)s, %(name)s, %(role)s::user_role_enum)",
        {"id": user_id, "identity_id": identity_id, "tenant_id": tenant_id, "name": name, "role": role},
    )


async def _insert_skill(skill_id: UUID, tenant_id: UUID | None, name: str) -> None:
    await _exec(
        "INSERT INTO skills ("
        "id, tenant_id, name, description, category, icon, folder_name, is_builtin, is_default"
        ") VALUES ("
        "%(id)s, %(tenant_id)s, %(name)s, 'uploaded', 'custom', 'S', %(folder)s, false, false"
        ")",
        {"id": skill_id, "tenant_id": tenant_id, "name": name, "folder": f"folder-{name}"},
    )


async def _insert_skill_file(file_id: UUID, skill_id: UUID, content: str) -> None:
    await _exec(
        "INSERT INTO skill_files (id, skill_id, path, content) VALUES (%(id)s, %(skill_id)s, 'SKILL.md', %(content)s)",
        {"id": file_id, "skill_id": skill_id, "content": content},
    )


async def _insert_template(template_id: UUID, created_by: UUID, name: str) -> None:
    await _exec(
        "INSERT INTO agent_templates ("
        "id, name, description, icon, category, soul_template, default_skills, "
        "default_mcp_servers, capability_bullets, is_builtin, created_by"
        ") VALUES ("
        "%(id)s, %(name)s, 'shared role', 'i', 'ops', 'soul', %(skills)s, %(mcp)s, %(bullets)s, false, %(created_by)s"
        ")",
        {
            "id": template_id,
            "name": name,
            "skills": Json(["shared"]),
            "mcp": Json([]),
            "bullets": Json(["keep"]),
            "created_by": created_by,
        },
    )


async def _insert_enterprise(info_id: UUID, info_type: str, updated_by: UUID) -> None:
    await _exec(
        "INSERT INTO enterprise_info (id, info_type, content, version, visible_roles, updated_by) "
        "VALUES (%(id)s, %(info_type)s, %(content)s, 1, %(roles)s, %(updated_by)s)",
        {
            "id": info_id,
            "info_type": info_type,
            "content": Json({"body": info_type}),
            "roles": Json(["org_admin"]),
            "updated_by": updated_by,
        },
    )


async def _insert_agent(agent_id: UUID, tenant_id: UUID, creator_id: UUID) -> None:
    await _exec(
        "INSERT INTO agents ("
        "id, name, role_description, creator_id, tenant_id, agent_type, max_triggers, "
        "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
        "max_llm_calls_per_day, heartbeat_interval_minutes"
        ") VALUES ("
        "%(id)s, 'Clerk', 'files', %(creator_id)s, %(tenant_id)s, 'openclaw', 20, 5, 5, "
        "'company', 'use', 1000, 240"
        ")",
        {"id": agent_id, "creator_id": creator_id, "tenant_id": tenant_id},
    )


async def _insert_department(department_id: UUID, tenant_id: UUID, user_id: UUID, name: str) -> None:
    await _exec(
        "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant_id)s, %(name)s)",
        {"id": department_id, "tenant_id": tenant_id, "name": name},
    )
    await _exec(
        "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
        "VALUES (%(user_id)s, %(tenant_id)s, %(department_id)s)",
        {"user_id": user_id, "tenant_id": tenant_id, "department_id": department_id},
    )


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return {str(key): item for key, item in loaded.items()}
    return {}


async def _sequence_was_called(statement: str) -> bool:
    row = await _one(statement)
    return bool(row and row["is_called"])


@pytest.mark.asyncio
async def test_delete_tenant_removes_uploaded_skills_and_keeps_shared_records(
    database: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del database
    departing_tenant = uuid4()
    keeper_tenant = uuid4()
    orphan_identity = uuid4()
    keeper_identity = uuid4()
    orphan_user = uuid4()
    keeper_user = uuid4()
    departing_skill = uuid4()
    departing_file = uuid4()
    keeper_skill = uuid4()
    keeper_file = uuid4()
    builtin_skill = uuid4()
    builtin_file = uuid4()
    departing_template = uuid4()
    keeper_template = uuid4()
    departing_info = uuid4()
    keeper_info = uuid4()
    agent_id = uuid4()
    orphan_email = f"orphan-{orphan_identity.hex[:8]}@example.com"

    await _insert_tenant(departing_tenant, "Departing")
    await _insert_tenant(keeper_tenant, "Keeper")
    await _insert_identity(orphan_identity, orphan_email, f"orphan-{orphan_identity.hex[:8]}", "5550001")
    await _insert_identity(
        keeper_identity, f"keeper-{keeper_identity.hex[:8]}@example.com", f"keep-{keeper_identity.hex[:8]}", "5550002"
    )
    await _insert_user(orphan_user, orphan_identity, departing_tenant, "org_admin", "Orphan Admin")
    await _insert_user(keeper_user, keeper_identity, keeper_tenant, "org_admin", "Keeper Admin")
    await _insert_skill(departing_skill, departing_tenant, f"depart-{departing_skill.hex[:8]}")
    await _insert_skill_file(departing_file, departing_skill, "departing skill")
    await _insert_skill(keeper_skill, keeper_tenant, f"keep-{keeper_skill.hex[:8]}")
    await _insert_skill_file(keeper_file, keeper_skill, "keeper skill")
    await _insert_skill(builtin_skill, None, f"builtin-{builtin_skill.hex[:8]}")
    await _insert_skill_file(builtin_file, builtin_skill, "builtin skill")
    await _insert_template(departing_template, orphan_user, f"Departing role {departing_template.hex[:8]}")
    await _insert_template(keeper_template, keeper_user, f"Keeper role {keeper_template.hex[:8]}")
    await _insert_enterprise(departing_info, f"departing-{departing_info.hex[:8]}", orphan_user)
    await _insert_enterprise(keeper_info, f"keeper-{keeper_info.hex[:8]}", keeper_user)
    await _insert_agent(agent_id, departing_tenant, orphan_user)

    published: list[str] = []

    async def record_version(key: str, *, ttl: int | None = None) -> None:
        del ttl
        published.append(f"ver:{key}")

    async def record_delete(*keys: str) -> None:
        published.append("del:" + ",".join(keys))

    async def record_acl(agent: UUID | None) -> None:
        published.append(f"acl:{agent}")

    monkeypatch.setattr(session_cache, "_ttl", lambda: 30)
    monkeypatch.setattr(tenant_cache, "_ttl", lambda: 30)
    monkeypatch.setattr(redis_cache, "_incr_version_now", record_version)
    monkeypatch.setattr(redis_cache, "cache_delete", record_delete)
    monkeypatch.setattr(access_cache, "_drop_acl_version_now", record_acl)

    actor = _actor(
        user_id=orphan_user,
        tenant_id=departing_tenant,
        identity_id=orphan_identity,
        role="org_admin",
        email=orphan_email,
    )
    async with connection_ctx() as db:
        response = await tenants_api.delete_tenant(departing_tenant, actor)
        assert published == []
        audit = await db.fetchone(
            "SELECT actor_id, tenant_id, actor_email, action, target_id, changes, details "
            "FROM admin_audit_logs WHERE action = 'tenant_delete'"
        )
        assert audit is not None
        assert audit["actor_id"] is None
        assert audit["tenant_id"] is None
        assert audit["actor_email"] == orphan_email
        assert audit["target_id"] == departing_tenant
        assert _json_object(audit["details"])["tenant_name"] == "Departing"
        assert await db.fetchval("SELECT email FROM identities WHERE id = %(id)s", {"id": orphan_identity}) is None

    assert response == {"status": "deleted", "fallback_tenant_id": None}
    assert any(item.startswith("ver:") for item in published)
    assert any(item.startswith("del:") for item in published)
    assert f"acl:{agent_id}" in published
    assert await _val("SELECT COUNT(*) FROM tenants WHERE id = %(id)s", {"id": departing_tenant}) == 0
    assert await _val("SELECT COUNT(*) FROM tenants WHERE id = %(id)s", {"id": keeper_tenant}) == 1
    assert await _val("SELECT COUNT(*) FROM skill_files WHERE id = %(id)s", {"id": departing_file}) == 0
    assert await _val("SELECT COUNT(*) FROM skills WHERE id = %(id)s", {"id": departing_skill}) == 0
    assert await _val("SELECT content FROM skill_files WHERE id = %(id)s", {"id": keeper_file}) == "keeper skill"
    assert await _val("SELECT content FROM skill_files WHERE id = %(id)s", {"id": builtin_file}) == "builtin skill"
    assert await _val("SELECT created_by FROM agent_templates WHERE id = %(id)s", {"id": departing_template}) is None
    assert (
        await _val("SELECT created_by FROM agent_templates WHERE id = %(id)s", {"id": keeper_template}) == keeper_user
    )
    assert await _val("SELECT updated_by FROM enterprise_info WHERE id = %(id)s", {"id": departing_info}) is None
    assert await _val("SELECT content FROM enterprise_info WHERE id = %(id)s", {"id": departing_info}) is not None
    assert await _val("SELECT updated_by FROM enterprise_info WHERE id = %(id)s", {"id": keeper_info}) == keeper_user
    assert await _val("SELECT COUNT(*) FROM users WHERE id = %(id)s", {"id": keeper_user}) == 1
    assert await _val("SELECT COUNT(*) FROM agents WHERE id = %(id)s", {"id": agent_id}) == 0
    orphan = await _one(
        "SELECT email, phone, username, password_hash, is_active FROM identities WHERE id = %(id)s",
        {"id": orphan_identity},
    )
    assert orphan is not None
    assert orphan["email"] is None
    assert orphan["phone"] is None
    assert orphan["username"] is None
    assert orphan["password_hash"] is None
    assert orphan["is_active"] is False
    keeper = await _one(
        "SELECT email, is_active, password_hash FROM identities WHERE id = %(id)s", {"id": keeper_identity}
    )
    assert keeper is not None
    assert keeper["email"] is not None
    assert keeper["is_active"] is True
    assert keeper["password_hash"] == "hash"
    await _insert_identity(uuid4(), orphan_email, f"reused-{orphan_identity.hex[:8]}", "5550009")


@pytest.mark.asyncio
async def test_delete_tenant_keeps_identities_that_still_have_memberships(database: None) -> None:
    del database
    departing_tenant = uuid4()
    keeper_tenant = uuid4()
    departing_identity = uuid4()
    keeper_identity = uuid4()
    departing_user = uuid4()
    keeper_user = uuid4()
    departing_department = uuid4()
    keeper_department = uuid4()
    keeper_email = f"member-{keeper_identity.hex[:8]}@example.com"

    await _insert_tenant(departing_tenant, "Closing")
    await _insert_tenant(keeper_tenant, "Remaining")
    await _insert_identity(
        departing_identity,
        f"gone-{departing_identity.hex[:8]}@example.com",
        f"gone-{departing_identity.hex[:8]}",
        "5550101",
    )
    await _insert_identity(keeper_identity, keeper_email, f"stay-{keeper_identity.hex[:8]}", "5550102")
    await _insert_user(departing_user, departing_identity, departing_tenant, "org_admin", "Closing Admin")
    await _insert_user(keeper_user, keeper_identity, keeper_tenant, "platform_admin", "Platform Admin")
    await _insert_department(departing_department, departing_tenant, departing_user, "Closing Desk")
    await _insert_department(keeper_department, keeper_tenant, keeper_user, "Remaining Desk")

    actor = _actor(
        user_id=keeper_user,
        tenant_id=keeper_tenant,
        identity_id=keeper_identity,
        role="platform_admin",
        email=keeper_email,
    )
    async with connection_ctx():
        response = await tenants_api.delete_tenant(departing_tenant, actor)

    assert response == {"status": "deleted", "fallback_tenant_id": str(keeper_tenant)}
    assert await _val("SELECT COUNT(*) FROM users WHERE id = %(id)s", {"id": keeper_user}) == 1
    assert await _val("SELECT COUNT(*) FROM users WHERE id = %(id)s", {"id": departing_user}) == 0
    assert (
        await _val(
            "SELECT COUNT(*) FROM local_department_memberships WHERE user_id = %(id)s",
            {"id": keeper_user},
        )
        == 1
    )
    assert (
        await _val(
            "SELECT COUNT(*) FROM local_department_memberships WHERE user_id = %(id)s",
            {"id": departing_user},
        )
        == 0
    )
    assert (
        await _val("SELECT name FROM local_departments WHERE id = %(id)s", {"id": keeper_department})
        == "Remaining Desk"
    )
    assert await _val("SELECT COUNT(*) FROM local_departments WHERE id = %(id)s", {"id": departing_department}) == 0
    assert await _val("SELECT email FROM identities WHERE id = %(id)s", {"id": keeper_identity}) == keeper_email
    assert await _val("SELECT email FROM identities WHERE id = %(id)s", {"id": departing_identity}) is None
    audit = await _one(
        "SELECT actor_id, tenant_id FROM admin_audit_logs WHERE action = 'tenant_delete'",
    )
    assert audit is not None
    assert audit["actor_id"] == keeper_user
    assert audit["tenant_id"] is None


@pytest.mark.asyncio
async def test_delete_tenant_continues_when_audit_insert_fails(database: None) -> None:
    del database
    tenant_id = uuid4()
    identity_id = uuid4()
    user_id = uuid4()
    skill_id = uuid4()
    file_id = uuid4()
    email = f"audit-{identity_id.hex[:8]}@example.com"
    await _insert_tenant(tenant_id, "Audited")
    await _insert_identity(identity_id, email, f"audit-{identity_id.hex[:8]}", "5550201")
    await _insert_user(user_id, identity_id, tenant_id, "org_admin", "Audit Admin")
    await _insert_skill(skill_id, tenant_id, f"audit-{skill_id.hex[:8]}")
    await _insert_skill_file(file_id, skill_id, "audit skill")
    await _exec("DROP SEQUENCE IF EXISTS tenant_delete_audit_fail_seq")
    await _exec("CREATE SEQUENCE tenant_delete_audit_fail_seq")
    await _exec(
        "CREATE OR REPLACE FUNCTION tenant_delete_audit_fail() RETURNS trigger "
        "LANGUAGE plpgsql AS $fn$ BEGIN PERFORM nextval('tenant_delete_audit_fail_seq'); "
        "RAISE EXCEPTION 'admin audit insert refused'; END $fn$"
    )
    await _exec("DROP TRIGGER IF EXISTS tenant_delete_audit_fail ON admin_audit_logs")
    await _exec(
        "CREATE TRIGGER tenant_delete_audit_fail BEFORE INSERT ON admin_audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION tenant_delete_audit_fail()"
    )
    attempted = False
    try:
        actor = _actor(
            user_id=user_id,
            tenant_id=tenant_id,
            identity_id=identity_id,
            role="org_admin",
            email=email,
        )
        async with connection_ctx():
            response = await tenants_api.delete_tenant(tenant_id, actor)
        attempted = await _sequence_was_called("SELECT is_called FROM tenant_delete_audit_fail_seq")
    finally:
        await _exec("DROP TRIGGER IF EXISTS tenant_delete_audit_fail ON admin_audit_logs")
        await _exec("DROP FUNCTION IF EXISTS tenant_delete_audit_fail()")
        await _exec("DROP SEQUENCE IF EXISTS tenant_delete_audit_fail_seq")

    assert attempted is True
    assert response == {"status": "deleted", "fallback_tenant_id": None}
    assert await _val("SELECT COUNT(*) FROM admin_audit_logs") == 0
    assert await _val("SELECT COUNT(*) FROM tenants WHERE id = %(id)s", {"id": tenant_id}) == 0
    assert await _val("SELECT COUNT(*) FROM skill_files WHERE id = %(id)s", {"id": file_id}) == 0
    assert await _val("SELECT email FROM identities WHERE id = %(id)s", {"id": identity_id}) is None


@pytest.mark.asyncio
async def test_delete_tenant_rolls_back_audit_when_a_later_delete_fails(database: None) -> None:
    del database
    departing_tenant = uuid4()
    keeper_tenant = uuid4()
    departing_identity = uuid4()
    keeper_identity = uuid4()
    departing_user = uuid4()
    keeper_user = uuid4()
    skill_id = uuid4()
    file_id = uuid4()
    template_id = uuid4()
    email = f"block-{departing_identity.hex[:8]}@example.com"
    await _insert_tenant(departing_tenant, "Blocked")
    await _insert_tenant(keeper_tenant, "Other")
    await _insert_identity(departing_identity, email, f"block-{departing_identity.hex[:8]}", "5550301")
    await _insert_identity(
        keeper_identity, f"other-{keeper_identity.hex[:8]}@example.com", f"other-{keeper_identity.hex[:8]}", "5550302"
    )
    await _insert_user(departing_user, departing_identity, departing_tenant, "org_admin", "Blocked Admin")
    await _insert_user(keeper_user, keeper_identity, keeper_tenant, "org_admin", "Other Admin")
    await _insert_skill(skill_id, departing_tenant, f"block-{skill_id.hex[:8]}")
    await _insert_skill_file(file_id, skill_id, "blocked skill")
    await _insert_template(template_id, departing_user, f"Blocked role {template_id.hex[:8]}")
    await _exec(
        "INSERT INTO invitation_codes (id, code, tenant_id, max_uses, used_count, created_by) "
        "VALUES (%(id)s, %(code)s, %(tenant_id)s, 1, 0, %(created_by)s)",
        {
            "id": uuid4(),
            "code": departing_user.hex[:16],
            "tenant_id": keeper_tenant,
            "created_by": departing_user,
        },
    )
    await _exec("DROP SEQUENCE IF EXISTS tenant_delete_audit_note_seq")
    await _exec("CREATE SEQUENCE tenant_delete_audit_note_seq")
    await _exec(
        "CREATE OR REPLACE FUNCTION tenant_delete_audit_note() RETURNS trigger "
        "LANGUAGE plpgsql AS $fn$ BEGIN PERFORM nextval('tenant_delete_audit_note_seq'); RETURN NEW; END $fn$"
    )
    await _exec("DROP TRIGGER IF EXISTS tenant_delete_audit_note ON admin_audit_logs")
    await _exec(
        "CREATE TRIGGER tenant_delete_audit_note AFTER INSERT ON admin_audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION tenant_delete_audit_note()"
    )
    attempted = False
    try:
        actor = _actor(
            user_id=departing_user,
            tenant_id=departing_tenant,
            identity_id=departing_identity,
            role="org_admin",
            email=email,
        )

        async def _delete() -> None:
            async with connection_ctx():
                await tenants_api.delete_tenant(departing_tenant, actor)

        with pytest.raises(ForeignKeyViolationError):
            await _delete()
        attempted = await _sequence_was_called("SELECT is_called FROM tenant_delete_audit_note_seq")
    finally:
        await _exec("DROP TRIGGER IF EXISTS tenant_delete_audit_note ON admin_audit_logs")
        await _exec("DROP FUNCTION IF EXISTS tenant_delete_audit_note()")
        await _exec("DROP SEQUENCE IF EXISTS tenant_delete_audit_note_seq")

    assert attempted is True
    assert await _val("SELECT COUNT(*) FROM admin_audit_logs") == 0
    assert await _val("SELECT COUNT(*) FROM tenants WHERE id = %(id)s", {"id": departing_tenant}) == 1
    assert await _val("SELECT content FROM skill_files WHERE id = %(id)s", {"id": file_id}) == "blocked skill"
    assert await _val("SELECT created_by FROM agent_templates WHERE id = %(id)s", {"id": template_id}) == departing_user
    identity = await _one(
        "SELECT email, is_active, password_hash FROM identities WHERE id = %(id)s",
        {"id": departing_identity},
    )
    assert identity is not None
    assert identity["email"] == email
    assert identity["is_active"] is True
    assert identity["password_hash"] == "hash"
    assert await _val("SELECT COUNT(*) FROM users WHERE id = %(id)s", {"id": keeper_user}) == 1
