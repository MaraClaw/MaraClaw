import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.api import users as users_api
from app.core.domain.users import UserRole
from app.core.security import create_access_token, get_current_user
from app.dao.user_dao import user_dao
from app.db import pool as pool_module
from app.db.connection import DbConnection
from app.db.pool import EnginePool
from app.db.session import bind_crud_connection
from app.records.user import UserRecord
from app.services import user_roster_tickets

TENANT = UUID(int=100)
FOREIGN = UUID(int=200)
NOW = datetime(2026, 10, 20, tzinfo=UTC)


class TicketRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int]] = {}
        self.now = 0

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = (value, self.now + ex)
        return True

    async def getdel(self, key: str) -> str | None:
        entry = self.values.pop(key, None)
        return entry[0] if entry and entry[1] > self.now else None


@dataclass(frozen=True, slots=True)
class RosterDatabase:
    writer: DbConnection
    pool: EnginePool
    actor: UserRecord


@asynccontextmanager
async def _roster_database(monkeypatch: pytest.MonkeyPatch, max_size: int) -> AsyncGenerator[RosterDatabase]:
    dsn = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("MARACLAW_TEST_POSTGRES_DSN is required for isolated roster snapshot QA")
    schema = f"roster_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row) as raw:
        writer = DbConnection(raw)
        await writer.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await writer.execute(f'SET search_path TO "{schema}"')
            await writer.execute((Path(__file__).parents[1] / "scripts/schema_baseline.sql").read_text())
            await writer.execute("ALTER TABLE users ALTER COLUMN created_at DROP NOT NULL")
            pool: EnginePool = AsyncConnectionPool(
                make_conninfo(dsn, options=f"-csearch_path={schema}"),
                min_size=1,
                max_size=max_size,
                timeout=2,
                open=False,
                kwargs={"row_factory": dict_row, "autocommit": False},
            )
            async with pool:
                monkeypatch.setattr(pool_module, "_pool", pool)
                monkeypatch.setattr(user_roster_tickets, "get_redis", AsyncMock(return_value=TicketRedis()))
                await writer.execute(
                    "INSERT INTO tenants (id, name, slug) VALUES (%(own)s, 'Own', 'own'), (%(foreign)s, 'Other', 'other')",
                    {"own": TENANT, "foreign": FOREIGN},
                )
                await writer.execute(
                    "INSERT INTO identities (id, email) VALUES (%(id)s, 'admin@example.com')",
                    {"id": UUID(int=1)},
                )
                await writer.execute(
                    "INSERT INTO users (id, identity_id, tenant_id, display_name, role) "
                    "VALUES (%(id)s, %(id)s, %(tenant)s, 'Admin', 'org_admin')",
                    {"id": UUID(int=1), "tenant": TENANT},
                )
                actor = await user_dao.get_with_identity(UUID(int=1), fresh=True)
                assert actor is not None
                yield RosterDatabase(writer, pool, actor)
        finally:
            await writer.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
async def roster_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RosterDatabase]:
    async with _roster_database(monkeypatch, max_size=3) as database:
        yield database


@pytest.fixture
async def roster_single_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RosterDatabase]:
    async with _roster_database(monkeypatch, max_size=1) as database:
        yield database


@pytest.fixture
async def roster_client(roster_db: RosterDatabase) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(users_api.export_router, prefix="/api")
    app.include_router(users_api.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = lambda: roster_db.actor
    token = await create_access_token(str(roster_db.actor.id), UserRole.ORG_ADMIN, identity_id=roster_db.actor.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


async def download_roster(client: httpx.AsyncClient, params: dict[str, str]) -> httpx.Response:
    issued = await client.post("/api/users/export", params=params)
    assert issued.status_code == 200
    handoff = user_roster_tickets.UserRosterExportOut.model_validate_json(issued.content)
    return await client.get("/api/users/export/download", params={"ticket": handoff.ticket})


async def seed_matches(database: RosterDatabase, count: int) -> None:
    await database.writer.executemany(
        "INSERT INTO users (id, tenant_id, display_name, role, created_at) "
        "VALUES (%(id)s, %(tenant)s, %(name)s, %(role)s, %(created)s)",
        [
            {
                "id": UUID(int=10 + index),
                "tenant": TENANT,
                "name": f"Match%_\\ {index}",
                "role": "org_admin" if index < 2 else "member",
                "created": NOW if index < count - 2 else None,
            }
            for index in range(count)
        ],
    )


async def seed_agent(database: RosterDatabase, creator: int, *, expired: bool = False, tenant: UUID = TENANT) -> None:
    await database.writer.execute(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, is_expired, status, "
        "max_triggers, min_poll_interval_min, webhook_rate_limit, access_mode, company_access_level, "
        "heartbeat_interval_minutes) VALUES (%(id)s, 'A', '', %(creator)s, %(tenant)s, %(expired)s, 'stopped', "
        "20, 5, 5, 'company', 'use', 240)",
        {"id": uuid4(), "creator": UUID(int=creator), "tenant": tenant, "expired": expired},
    )
