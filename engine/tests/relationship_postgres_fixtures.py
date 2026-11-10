import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import okr, relationships
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
                tenant = await tenant_dao.create(obj_in={"name": "Relationship QA", "slug": uuid4().hex})
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
