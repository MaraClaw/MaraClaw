from datetime import timedelta
from uuid import UUID

import anyio
import httpx
import pytest
from fastapi import FastAPI
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from tests.search_analytics_postgres_fixtures import (
    END,
    MISSING,
    START,
    TENANT,
    AnalyticsDatabase,
    analytics_db,
    legacy_dashboard,
    seed_mixed_events,
)

from app.api import admin_search_analytics as analytics
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.web_search_analytics_sql import SearchAnalyticsDashboardProjection
from app.dao.web_search_event_dao import web_search_event_dao
from app.db.connection import DbConnection
from app.records.user import UserRecord

__all__ = ["analytics_db"]


@pytest.mark.parametrize("tenant_id", [None, TENANT, MISSING, UUID(int=999)])
async def test_dashboard_matches_legacy_postgres_aggregations(
    analytics_db: AnalyticsDatabase, tenant_id: UUID | None
) -> None:
    await seed_mixed_events(analytics_db.db)
    expected = await legacy_dashboard(tenant_id)

    result = await web_search_event_dao.dashboard(start=START, end=END, tenant_id=tenant_id)

    assert result["system_summary"] == expected["system_summary"]
    assert result["selected_summary"] == expected["selected_summary"]
    assert result["timeseries"] == expected["timeseries"]
    assert sorted(result["orgs"], key=lambda row: row["tenant_id"] or "") == sorted(
        expected["orgs"], key=lambda row: row["tenant_id"] or ""
    )
    assert sorted(result["trending"], key=lambda row: (row["query_hash"], row["kind"])) == sorted(
        expected["trending"], key=lambda row: (row["query_hash"], row["kind"])
    )
    assert [row["event_count"] for row in result["orgs"]] == sorted(
        [row["event_count"] for row in result["orgs"]], reverse=True
    )
    summary = result["system_summary"]
    assert (summary["event_count"], summary["unique_orgs"], summary["unique_agents"]) == (7, 3, 1)
    assert (summary["error_count"], summary["quota_count"], summary["unattributed_count"]) == (2, 4, 2)
    assert summary["avg_latency_ms"] == pytest.approx(6125 / 7)
    if tenant_id is None:
        assert result["selected_summary"] == summary
        assert result["timeseries"][0] == {"date": "2026-10-20", "event_count": 3, "error_count": 1, "quota_count": 1}
        ranks = [(row["distinct_orgs"], row["hits"]) for row in result["trending"]]
    else:
        ranks = [(row["hits"],) for row in result["trending"]]
    assert ranks == sorted(ranks, reverse=True)


@pytest.mark.parametrize("tenant_id", [None, TENANT])
@pytest.mark.parametrize("empty", [False, True])
async def test_dashboard_window_and_http_surface(
    analytics_db: AnalyticsDatabase, tenant_id: UUID | None, empty: bool
) -> None:
    await seed_mixed_events(analytics_db.db)
    app = FastAPI()
    app.include_router(analytics.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=UUID(int=1), role=UserRole.PLATFORM_ADMIN)
    start, end = (END + timedelta(days=1), END + timedelta(days=2)) if empty else (START, END)
    params = {"start": start.isoformat(), "end": end.isoformat()}
    if tenant_id is not None:
        params["tenant_id"] = str(tenant_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://postgres-analytics"
    ) as client:
        response = await client.get("/api/admin/search-analytics/dashboard", params=params)
    app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "private-raw-export-sentinel" not in response.text
    body = response.json()
    if empty:
        assert body["system_summary"]["event_count"] == body["selected_summary"]["event_count"] == 0
        assert body["system_summary"]["avg_latency_ms"] == 0
        assert body["system_summary"]["by_kind"] == body["selected_summary"]["by_kind"] == []
        assert body["timeseries"] == body["orgs"] == body["trending"] == []
    else:
        assert body["system_summary"]["event_count"] == 7
        assert body["selected_summary"]["event_count"] == (3 if tenant_id is not None else 7)
        assert sum(row["event_count"] for row in body["orgs"]) == 5
        assert body["trending"][0]["query_normalized"] == "shared"


async def test_dashboard_uses_one_snapshot_during_concurrent_insert(analytics_db: AnalyticsDatabase) -> None:
    db = analytics_db.db
    await seed_mixed_events(db)
    expected = await legacy_dashboard(TENANT)
    await db.execute("ALTER TABLE web_search_events RENAME TO event_store")
    await db.execute(
        "CREATE FUNCTION analytics_barrier() RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$ "
        "BEGIN PERFORM pg_advisory_xact_lock_shared(741852); RETURN true; END $$"
    )
    await db.execute("CREATE VIEW web_search_events AS SELECT * FROM event_store WHERE analytics_barrier()")
    snapshots: list[SearchAnalyticsDashboardProjection] = []

    async def read_dashboard() -> None:
        snapshots.append(await web_search_event_dao.dashboard(start=START, end=END, tenant_id=TENANT))

    async with await AsyncConnection[DictRow].connect(analytics_db.dsn, autocommit=True, row_factory=dict_row) as raw:
        writer = DbConnection(raw)
        await writer.execute(f'SET search_path TO "{analytics_db.schema}"')
        await writer.execute("SELECT pg_advisory_lock(741852)")
        try:
            with anyio.fail_after(10):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(read_dashboard)
                    while not await writer.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                        "AND objid = 741852 AND mode = 'ShareLock' AND NOT granted)"
                    ):
                        await anyio.lowlevel.checkpoint()
                    await writer.execute(
                        "INSERT INTO event_store SELECT gen_random_uuid(), occurred_at, agent_id, tenant_id, kind, "
                        "billed, method, http_status, status_class, latency_ms, key_id, query_hash, query_normalized, "
                        "query_char_len, depth, output_type, primary_domain, result_count, error_class, request_bytes, "
                        "response_bytes, export_state, export_claimed_at, exported_at, schema_version "
                        "FROM event_store WHERE occurred_at = %(start)s",
                        {"start": START},
                    )
                    await writer.execute("SELECT pg_advisory_unlock(741852)")
        finally:
            await writer.execute("SELECT pg_advisory_unlock(741852)")
    assert snapshots[0]["system_summary"] == expected["system_summary"]
    assert snapshots[0]["selected_summary"] == expected["selected_summary"]
    assert snapshots[0]["timeseries"] == expected["timeseries"]
    assert sorted(snapshots[0]["orgs"], key=lambda row: row["tenant_id"] or "") == sorted(
        expected["orgs"], key=lambda row: row["tenant_id"] or ""
    )
    assert sorted(snapshots[0]["trending"], key=lambda row: (row["query_hash"], row["kind"])) == sorted(
        expected["trending"], key=lambda row: (row["query_hash"], row["kind"])
    )
    after = await web_search_event_dao.dashboard(start=START, end=END, tenant_id=TENANT)
    assert after["system_summary"]["event_count"] == snapshots[0]["system_summary"]["event_count"] + 1


async def test_dashboard_retains_ranking_limits_with_high_diversity(analytics_db: AnalyticsDatabase) -> None:
    await analytics_db.db.execute(
        "INSERT INTO web_search_events (id,occurred_at,tenant_id,agent_id,kind,method,http_status,status_class,"
        "latency_ms,query_hash,query_normalized) SELECT gen_random_uuid(), %(start)s, "
        "md5((i %% 30)::text)::uuid, NULL, 'search', 'POST', 200, 'ok', i, i::text, i::text "
        "FROM generate_series(1, 100) i",
        {"start": START},
    )
    result = await web_search_event_dao.dashboard(start=START, end=END, tenant_id=None)
    assert len(result["orgs"]) == 20
    assert len(result["trending"]) == 50
    assert result["system_summary"]["event_count"] == 100
    assert result["system_summary"]["unique_orgs"] == 30
    assert result["system_summary"]["unique_agents"] == 0
