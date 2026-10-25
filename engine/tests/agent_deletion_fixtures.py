"""Live-PostgreSQL ASGI scenario for agent deletion requests and ownership transfers."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import advanced, agent_deletion_requests, agent_ownership_transfers, agents, background_tasks, users
from app.core import row_memo
from app.core.security import create_access_token, hash_password_async
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection, Row
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection
from app.records.agent import AgentRecord
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord
from app.records.user import UserRecord
from app.services import agent_deletion_cleanup, agent_stops
from app.services.agent_deletion_archive import publish_deletion_archive
from app.services.agent_manager import agent_manager

BASELINE = Path(__file__).resolve().parents[1] / "scripts" / "schema_baseline.sql"


@dataclass
class DeletionScenario:
    db: DbConnection
    client: AsyncClient
    tenant_id: UUID
    foreign_tenant_id: UUID
    people: dict[str, UserRecord]
    tokens: dict[UUID, str]
    runtime_calls: list[tuple[str, UUID]] = field(default_factory=list)

    def __getitem__(self, name: str) -> UserRecord:
        return self.people[name]

    def headers(self, name: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[self.people[name].id]}"}

    async def call(self, who: str, method: str, path: str, json: dict[str, object] | None = None) -> Response:
        return await self.client.request(method, f"/api{path}", headers=self.headers(who), json=json)

    async def rows(self, query: str, params: dict[str, object] | None = None) -> list[Row]:
        return await self.db.fetchall(query, params)

    async def agent(
        self,
        owner: str,
        name: str = "Ops Bot",
        mode: str = "company",
        *,
        is_system: bool = False,
        is_expired: bool = False,
        status: str = "idle",
        tenant_id: UUID | None = None,
    ) -> UUID:
        agent_id = uuid4()
        values = {
            "id": agent_id,
            "name": name,
            "creator_id": self[owner].id,
            "tenant_id": tenant_id or self.tenant_id,
            "mode": mode,
            "is_system": is_system,
            "status": status,
            "is_expired": is_expired,
        }
        await self.db.execute(
            "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, agent_type, max_triggers, "
            "min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, max_llm_calls_per_day, "
            "heartbeat_interval_minutes, is_system, status, is_expired) VALUES (%(id)s, %(name)s, 'Role', "
            "%(creator_id)s, %(tenant_id)s, 'openclaw', 1, 5, 5, %(mode)s, 'use', 100, 240, %(is_system)s, "
            "%(status)s, %(is_expired)s)",
            values,
        )
        if mode in ("private", "custom"):
            await self.db.execute(
                "INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level) "
                "VALUES (%(id)s, %(agent)s, 'user', %(user)s, 'manage')",
                {"id": uuid4(), "agent": agent_id, "user": self[owner].id},
            )
        return agent_id

    async def request(self, who: str, agent_id: UUID, reason: str = "No longer needed") -> Response:
        return await self.call(
            who, "POST", "/agent-deletion-requests", json={"agent_id": str(agent_id), "reason": reason}
        )

    async def approve(self, who: str, request_id: str, *, view: bool = True) -> Response:
        if view:
            viewed = await self.call(who, "GET", f"/agent-deletion-requests/{request_id}")
            assert viewed.status_code == 200, viewed.text
        return await self.call(who, "POST", f"/agent-deletion-requests/{request_id}/approve")

    async def agent_exists(self, agent_id: UUID) -> bool:
        return bool(await self.rows("SELECT 1 FROM agents WHERE id = %(id)s", {"id": agent_id}))

    async def set_user(self, who: str, **columns: object) -> None:
        assignments = ", ".join(f"{column} = %({column})s" for column in columns)
        await self.db.execute(f"UPDATE users SET {assignments} WHERE id = %(id)s", {"id": self[who].id, **columns})  # noqa: S608


_ROLES = (
    ("genesis", "org_admin", True),
    ("oa2", "org_admin", False),
    ("oa3", "org_admin", False),
    ("owner", "member", False),
    ("other", "member", False),
    ("target", "member", False),
    ("helper", "agent_admin", False),
    ("psa", "platform_admin", False),
)


@pytest.fixture
async def deletion_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[DeletionScenario]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("MARACLAW_TEST_POSTGRES_DSN required for isolated PostgreSQL agent deletion tests")
    schema = f"agent_deletion_{uuid4().hex}"
    monkeypatch.setenv("USER_SESSION_CACHE_TTL_SECONDS", "0")
    monkeypatch.setenv("TENANT_CACHE_TTL_SECONDS", "0")
    async with await AsyncConnection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(BASELINE.read_text())
            await init_pool(conninfo=make_conninfo(dsn, options=f"-csearch_path={schema}"), min_size=1, max_size=8)
            tenant = await tenant_dao.create(obj_in={"name": "Acme", "slug": "acme"})
            foreign = await tenant_dao.create(obj_in={"name": "Other", "slug": "other"})

            password_hash = await hash_password_async("qa-password")

            async def member(name: str, tenant_id: UUID, role: str, genesis: bool) -> UserRecord:
                identity = await identity_dao.create(
                    obj_in={
                        "email": f"{name}@{tenant_id.hex[:6]}.test",
                        "email_verified": True,
                        "password_hash": password_hash,
                        "is_platform_admin": False,
                    }
                )
                user = await user_dao.create(
                    obj_in={
                        "identity_id": identity.id,
                        "tenant_id": tenant_id,
                        "display_name": name,
                        "role": role,
                        "is_genesis": genesis,
                        "quota_max_agents": 2,
                    }
                )
                user.identity = identity
                return user

            people = {name: await member(name, tenant.id, role, genesis) for name, role, genesis in _ROLES}
            people["foreign_oa"] = await member("foreign_oa", foreign.id, "org_admin", True)
            tokens = {user.id: await create_access_token(str(user.id), user.role) for user in people.values()}

            runtime_calls: list[tuple[str, UUID]] = []

            async def remove_container(agent: AgentRecord) -> bool:
                runtime_calls.append(("remove", agent.id))
                return True

            def archive_files(job: AgentDeletionCleanupRecord) -> None:
                runtime_calls.append(("archive", job.agent_id))
                publish_deletion_archive(job)

            monkeypatch.setattr(agent_manager, "remove_container", remove_container)
            monkeypatch.setattr(
                agent_manager, "docker_client", SimpleNamespace(container=SimpleNamespace(list=lambda **_: []))
            )
            monkeypatch.setattr(
                agent_stops, "deletion_archive_path", lambda agent_id: tmp_path / "archive" / str(agent_id)
            )
            monkeypatch.setattr(agent_deletion_cleanup, "publish_deletion_archive", archive_files)
            monkeypatch.setattr(agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))

            app = FastAPI()
            for router in (
                agents.router,
                agent_deletion_requests.router,
                agent_ownership_transfers.router,
                advanced.router,
                users.router,
            ):
                app.include_router(router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
            row_memo.clear_row_memo()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://deletion.test") as client:
                yield DeletionScenario(db, client, tenant.id, foreign.id, people, tokens, runtime_calls)
            await asyncio.gather(*tuple(background_tasks._tasks))
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
