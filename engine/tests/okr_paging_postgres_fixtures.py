"""Isolated, opt-in PostgreSQL dataset for the real OKR HTTP routes."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import orjson
import pytest
from anyio import Path
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.api import okr
from app.config import get_settings
from app.core.security import create_access_token
from app.db import session as session_module
from app.db.connection import DbConnection, Params, Row
from app.db.pool import close_pool, init_pool
from app.db.session import bind_crud_connection
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies

COUNT: Final = 600
START: Final = date(2026, 1, 1)
END: Final = date(2026, 3, 31)
CLOCK: Final = datetime(2026, 1, 2, tzinfo=UTC)
OWNER_TYPES: Final = ("agent", "company", "user")
PERIOD: Final = {"period_start": START.isoformat(), "period_end": END.isoformat()}
PAGE_PATH: Final = "/api/okr/objectives/page"
LEGACY_PATH: Final = "/api/okr/objectives"


def objective_id(index: int) -> UUID:
    return UUID(int=1000 + index)


def owner_id(index: int) -> UUID | None:
    return None if index % 3 == 1 else UUID(int=10_000 + index)


def kr_ids(index: int) -> list[str]:
    return [str(UUID(int=100_000 + index * 1000 + kr)) for kr in range(205 if index == 0 else 3)]


@dataclass(frozen=True, slots=True)
class OKRPostgres:
    db: DbConnection
    client: AsyncClient
    schema: str
    tenant_id: UUID
    other_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class SQLRead:
    sql: str
    params: Params
    rows: int


@pytest.fixture
def sql_reads(monkeypatch: pytest.MonkeyPatch) -> list[SQLRead]:
    """Observe executed SQL and actual returned rows without substituting any result."""
    reads: list[SQLRead] = []
    fetchall = DbConnection.fetchall

    async def observed(db: DbConnection, query: str, params: Params = None) -> list[Row]:
        rows = await fetchall(db, query, params)
        reads.append(SQLRead(query, params, len(rows)))
        return rows

    monkeypatch.setattr(DbConnection, "fetchall", observed)
    return reads


async def _seed(db: DbConnection, tenant_ids: tuple[UUID, UUID]) -> tuple[str, str]:
    tokens: list[str] = []
    for tenant_id in tenant_ids:
        identity, user = uuid4(), uuid4()
        await db.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%(id)s, 'OKR QA', %(slug)s)",
            {"id": tenant_id, "slug": tenant_id.hex},
        )
        await db.execute("INSERT INTO identities (id) VALUES (%(id)s)", {"id": identity})
        await db.execute(
            "INSERT INTO users (id, identity_id, tenant_id, display_name) VALUES (%(id)s,%(identity)s,%(tenant)s,'Reader')",
            {"id": user, "identity": identity, "tenant": tenant_id},
        )
        tokens.append(await create_access_token(str(user), "member", identity_id=identity))
    creator = await db.fetchval("SELECT id FROM users WHERE tenant_id = %(id)s", {"id": tenant_ids[0]})
    await db.executemany(
        "INSERT INTO users (id, tenant_id, display_name) VALUES (%(id)s,%(tenant)s,%(name)s)",
        [{"id": owner_id(i), "tenant": tenant_ids[0], "name": f"User {i}"} for i in range(COUNT) if i % 3 == 2],
    )
    await db.executemany(
        "INSERT INTO agents (id, name, role_description, creator_id, tenant_id, max_triggers, min_poll_interval_min, "
        "webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes) "
        "VALUES (%(id)s,%(name)s,'QA',%(creator)s,%(tenant)s,10,5,10,'private','use',30)",
        [
            {"id": owner_id(i), "name": f"Agent {i}", "creator": creator, "tenant": tenant_ids[0]}
            for i in range(COUNT)
            if i % 3 == 0
        ],
    )
    objectives: list[Params] = [
        {
            "id": objective_id(i),
            "tenant": tenant_ids[0],
            "title": f"Objective {i}",
            "owner_type": OWNER_TYPES[i % 3],
            "owner_id": owner_id(i),
            "status": "active",
            "start": START + timedelta(days=1) if i == 1 else START,
            "end": END - timedelta(days=1) if i == 1 else END,
            "created": CLOCK,
        }
        for i in range(COUNT)
    ]
    # Archived, overlapping-start, overlapping-end and foreign-tenant rows must all disappear.
    objectives.extend(
        {
            "id": UUID(int=i + 1),
            "tenant": tenant_ids[1] if i == 3 else tenant_ids[0],
            "title": "Excluded",
            "owner_type": "company",
            "owner_id": None,
            "status": "archived" if i == 0 else "active",
            "start": START - timedelta(days=1) if i == 1 else START,
            "end": END + timedelta(days=1) if i == 2 else END,
            "created": CLOCK,
        }
        for i in range(4)
    )
    await db.executemany(
        "INSERT INTO okr_objectives (id,tenant_id,title,description,owner_type,owner_id,period_start,period_end,"
        "status,created_at) VALUES (%(id)s,%(tenant)s,%(title)s,repeat('detail ',20),%(owner_type)s,%(owner_id)s,"
        "%(start)s,%(end)s,%(status)s,%(created)s)",
        objectives,
    )
    await db.executemany(
        "INSERT INTO okr_key_results (id,objective_id,title,target_value,current_value,status,created_at,last_updated_at) "
        "VALUES (%(id)s,%(objective)s,%(title)s,100,25,'on_track',%(created)s,%(created)s)",
        [
            {
                "id": UUID(key),
                "objective": objective_id(i),
                "title": f"KR {i}/{k}",
                "created": CLOCK + timedelta(microseconds=i * 1000 + k),
            }
            for i in range(COUNT)
            for k, key in enumerate(kr_ids(i))
        ],
    )
    for table in ("okr_objectives", "okr_key_results", "users", "agents"):
        await db.execute(f"ANALYZE {table}")
    return tokens[0], tokens[1]


@pytest.fixture
async def okr_postgres(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[OKRPostgres]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL OKR QA")
    schema = f"okr_paging_qa_{uuid4().hex}"
    context_token = session_module._conn_ctx.set(None)
    monkeypatch.setattr(get_settings(), "USER_SESSION_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(get_settings(), "TENANT_CACHE_TTL_SECONDS", 0)
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                await db.execute(statement)
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=4)
            tenant_ids = (uuid4(), uuid4())
            token, other_token = await _seed(db, tenant_ids)
            app = FastAPI()
            app.include_router(okr.router, dependencies=[Depends(bind_crud_connection)])
            async with AsyncClient(
                transport=ASGITransport(app),
                base_url="http://okr-postgres",
                timeout=30,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                yield OKRPostgres(db, client, schema, tenant_ids[0], {"Authorization": f"Bearer {other_token}"})
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')
            remaining = await db.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = %(schema)s)", {"schema": schema}
            )
            assert remaining is False
            session_module._conn_ctx.reset(context_token)
            if (evidence_dir := os.environ.get("OKR_PAGING_EVIDENCE_DIR")) is not None:
                path = Path(evidence_dir)
                await path.mkdir(parents=True, exist_ok=True)
                async with await (path / "teardown.jsonl").open("a") as receipt:
                    await receipt.write(
                        orjson.dumps({"schema": schema, "pool_closed": True, "schema_exists": remaining}).decode()
                        + "\n"
                    )
