import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import okr, relationships
from app.core import access_cache
from app.core.acl_locks import lock_tenants
from app.core.security import get_current_user
from app.dao import agent_dao, org_member_dao, tenant_dao, user_dao
from app.dao.okr_settings_dao import okr_settings_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection, connection_ctx
from app.records.agent import AgentRecord
from app.records.org import OrgMemberRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies


@dataclass(frozen=True, slots=True)
class RelationshipDatabase:
    tenant_id: UUID
    actor: UserRecord
    agent: AgentRecord
    members: tuple[OrgMemberRecord, ...]
    client: AsyncClient

    @property
    def path(self) -> str:
        return f"/api/agents/{self.agent.id}/relationships/"


@pytest.fixture
async def relationship_database() -> AsyncIterator[RelationshipDatabase]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL contention QA")
    schema = f"relationship_qa_{uuid4().hex}"
    token = session_module._conn_ctx.set(None)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        admin = DbConnection(raw)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await admin.execute(f'SET search_path TO "{schema}"')
            for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                await admin.execute(statement)
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=6)
            async with connection_ctx():
                tenant = await tenant_dao.create(
                    obj_in={"name": "Relationship QA", "slug": uuid4().hex, "tier": OrganizationPlanId.ULTRA}
                )
                actor = await user_dao.create(
                    obj_in={"display_name": "Manager", "tenant_id": tenant.id, "role": "org_admin", "is_active": True}
                )
                agent = await agent_dao.create(
                    obj_in={"name": "OKR Agent", "creator_id": actor.id, "tenant_id": tenant.id, "is_system": True}
                )
                settings = await okr_settings_dao.get_or_create(tenant.id)
                await okr_settings_dao.update(db_obj=settings, obj_in={"enabled": True, "okr_agent_id": agent.id})
                members = tuple(
                    [
                        await org_member_dao.create(
                            obj_in={"name": f"Contact {index}", "tenant_id": tenant.id, "status": "active"}
                        )
                        for index in range(5)
                    ]
                )
            app = FastAPI()
            app.include_router(relationships.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
            app.include_router(okr.router, dependencies=[Depends(bind_crud_connection)])
            app.dependency_overrides[get_current_user] = lambda: actor
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://relationships-qa"
                ) as client:
                    yield RelationshipDatabase(tenant.id, actor, agent, members, client)
            finally:
                app.dependency_overrides.clear()
        finally:
            await close_pool()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            session_module._conn_ctx.reset(token)


async def serialize_contenders(
    database: RelationshipDatabase, first: Callable[[], Awaitable[None]], second: Callable[[], Awaitable[None]]
) -> None:
    first_written, second_entered, release_first, second_done = (anyio.Event() for _ in range(4))
    second_pid: list[int] = []

    async def holder() -> None:
        async with connection_ctx() as db:
            await lock_tenants(db, (database.tenant_id,))
            await first()
            first_written.set()
            await release_first.wait()

    async def waiter() -> None:
        await first_written.wait()
        async with connection_ctx() as db:
            pid = await db.fetchval("SELECT pg_backend_pid()")
            assert isinstance(pid, int)
            second_pid.append(pid)
            second_entered.set()
            await second()
        second_done.set()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(holder)
            group.start_soon(waiter)
            await second_entered.wait()
            async with connection_ctx() as observer:
                while True:
                    blockers = await observer.fetchval("SELECT pg_blocking_pids(%(pid)s)", {"pid": second_pid[0]})
                    if blockers:
                        break
                    assert not second_done.is_set(), "Competing writer bypassed the transaction lock"
                    await anyio.lowlevel.checkpoint()
            release_first.set()


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement a ``DbConnection`` runs, in order; clear it before the call being measured."""
    seen: list[str] = []
    for method in ("fetchall", "fetchone", "fetchval", "execute", "executemany"):
        original = getattr(DbConnection, method)

        def counted(self: DbConnection, query: str, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            seen.append(query)
            return _original(self, query, *args, **kwargs)

        monkeypatch.setattr(DbConnection, method, counted)
    return seen


@pytest.fixture
def access_cache_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The decision cache is enabled but Redis is down, so every read and fill fails open to SQL."""

    async def unavailable() -> Any:
        raise ConnectionError("access cache unavailable")

    monkeypatch.setattr(access_cache, "_ttl_seconds", lambda: 45)
    monkeypatch.setattr(access_cache, "get_cache_redis", unavailable)


async def add_tenant(name: str) -> UUID:
    tenant = await tenant_dao.create(obj_in={"name": name, "slug": uuid4().hex, "tier": OrganizationPlanId.ULTRA})
    return tenant.id


async def add_user(tenant_id: UUID, name: str, *, role: str = "member", active: bool = True) -> UserRecord:
    return await user_dao.create(
        obj_in={"display_name": name, "tenant_id": tenant_id, "role": role, "is_active": active}
    )


async def add_agent(
    tenant_id: UUID,
    creator_id: UUID,
    name: str,
    *,
    mode: str = "custom",
    level: str = "use",
    fields: Mapping[str, object] | None = None,
) -> AgentRecord:
    return await agent_dao.create(
        obj_in={
            "name": name,
            "creator_id": creator_id,
            "tenant_id": tenant_id,
            "access_mode": mode,
            "company_access_level": level,
            **(fields or {}),
        }
    )


async def add_member(
    tenant_id: UUID | None, name: str, *, user_id: UUID | None = None, status: str = "active"
) -> OrgMemberRecord:
    return await org_member_dao.create(
        obj_in={"name": name, "tenant_id": tenant_id, "status": status, "user_id": user_id}
    )


async def grant_user(agent_id: UUID, user_id: UUID, level: str) -> None:
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
            "VALUES (%(id)s, %(agent)s, 'user', %(user)s, %(level)s)",
            {"id": uuid4(), "agent": agent_id, "user": user_id, "level": level},
        )


async def grant_department(tenant_id: UUID, agent_id: UUID, user_id: UUID) -> None:
    """Put the user in a new department that holds a ``use`` grant on the agent."""
    department = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO local_departments (id, tenant_id, name) VALUES (%(id)s, %(tenant)s, %(name)s)",
            {"id": department, "tenant": tenant_id, "name": f"Department {department}"},
        )
        await db.execute(
            "INSERT INTO local_department_memberships (user_id, tenant_id, department_id) "
            "VALUES (%(user)s, %(tenant)s, %(department)s)",
            {"user": user_id, "tenant": tenant_id, "department": department},
        )
        await db.execute(
            "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
            "VALUES (%(id)s, %(agent)s, 'department', %(department)s, 'use')",
            {"id": uuid4(), "agent": agent_id, "department": department},
        )
