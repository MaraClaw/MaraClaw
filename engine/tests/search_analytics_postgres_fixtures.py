import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

from app.dao.web_search_analytics_sql import SearchAnalyticsDashboardProjection
from app.dao.web_search_event_dao import web_search_event_dao
from app.db import session as session_module
from app.db.connection import DbConnection
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies

START = datetime(2026, 10, 20, tzinfo=UTC)
END = START + timedelta(days=7)
TENANT = UUID(int=10)
OTHER = UUID(int=11)
MISSING = UUID(int=12)
AGENT = UUID(int=20)


@dataclass(frozen=True, slots=True)
class AnalyticsDatabase:
    db: DbConnection
    dsn: str
    schema: str


@pytest.fixture
async def analytics_db() -> AsyncIterator[AnalyticsDatabase]:
    dsn = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL analytics QA")
    schema = f"search_analytics_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(dsn, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        token = session_module._conn_ctx.set(db)
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute("SET timezone = 'Pacific/Honolulu'")
            await db.execute("CREATE TABLE tenants (id uuid PRIMARY KEY, name varchar(100) NOT NULL)")
            for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
                if statement.startswith(
                    (
                        "CREATE TABLE IF NOT EXISTS web_search_events (",
                        "CREATE TABLE IF NOT EXISTS web_search_export_payloads (",
                    )
                ) or (statement.startswith("CREATE INDEX") and "ON web_search_events " in statement):
                    await db.execute(statement)
            await db.execute(
                "INSERT INTO tenants VALUES (%(tenant)s, 'Selected'), (%(other)s, '')",
                {"tenant": TENANT, "other": OTHER},
            )
            yield AnalyticsDatabase(db, dsn, schema)
        finally:
            session_module._conn_ctx.reset(token)
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def seed_mixed_events(db: DbConnection) -> None:
    for tenant, agent, kind, status, error, latency, query, when in (
        (TENANT, AGENT, "search", "ok", None, 10, "shared", START),
        (TENANT, AGENT, "search", "ok", "quota_rotated", 20, "shared", START + timedelta(hours=1)),
        (TENANT, AGENT, "crawl", "quota", None, 90, "shared", START + timedelta(days=1)),
        (OTHER, AGENT, "search", "upstream", "timeout", 1000, "shared", START + timedelta(hours=23, minutes=59)),
        (MISSING, None, "future-kind", "quota", None, 2000, "rare", START + timedelta(days=2)),
        (None, None, "search", "ok", None, 3000, "", START + timedelta(days=1)),
        (None, AGENT, "search", "error", "quota_rotated", 5, "solo", START + timedelta(days=1)),
        (TENANT, AGENT, "search", "error", None, 999999, "outside", START - timedelta(microseconds=1)),
        (TENANT, AGENT, "search", "error", None, 999999, "outside", END),
    ):
        await db.execute(
            "INSERT INTO web_search_events "
            "(id,occurred_at,tenant_id,agent_id,kind,method,http_status,status_class,error_class,latency_ms,"
            "query_hash,query_normalized) VALUES "
            "(%(id)s,%(when)s,%(tenant)s,%(agent)s,%(kind)s,'POST',200,%(status)s,%(error)s,%(latency)s,%(query)s,%(query)s)",
            {
                "id": uuid4(),
                "when": when,
                "tenant": tenant,
                "agent": agent,
                "kind": kind,
                "status": status,
                "error": error,
                "latency": latency,
                "query": query,
            },
        )
    await db.execute(
        "INSERT INTO web_search_export_payloads (event_id, raw_query) "
        "SELECT id, 'private-raw-export-sentinel@example.com' FROM web_search_events"
    )


async def legacy_dashboard(tenant_id: UUID | None) -> SearchAnalyticsDashboardProjection:
    system = await web_search_event_dao.summary(start=START, end=END, tenant_id=None)
    selected = (
        system if tenant_id is None else await web_search_event_dao.summary(start=START, end=END, tenant_id=tenant_id)
    )
    return {
        "system_summary": system,
        "selected_summary": selected,
        "timeseries": await web_search_event_dao.timeseries(start=START, end=END, tenant_id=tenant_id),
        "orgs": await web_search_event_dao.top_orgs(start=START, end=END, limit=20),
        "trending": await web_search_event_dao.trending(
            start=START, end=END, tenant_id=tenant_id, limit=50, system_wide=tenant_id is None
        ),
    }
