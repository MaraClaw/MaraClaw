from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

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
from app.db.connection import DbConnection, Params, Row
from app.records.user import UserRecord
from app.scripts.bootstrap_db import SCHEMA_BASELINE, _statement_bodies

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


CUTOFF = datetime(2026, 11, 1, tzinfo=UTC)
OLD = CUTOFF - timedelta(days=3)
RECENT = CUTOFF + timedelta(days=3)
# A purge that waits on a locked row fails after this long instead of hanging the run.
FAIL_AFTER_SECONDS = 10


async def add_event(db: DbConnection, occurred_at: datetime, *, state: str = "exported") -> UUID:
    event_id = uuid4()
    await db.execute(
        "INSERT INTO web_search_events (id, occurred_at, kind, method, http_status, status_class, latency_ms, "
        "query_hash, export_state) VALUES (%(id)s, %(at)s, 'search', 'POST', 200, 'ok', 5, 'hash', %(state)s)",
        {"id": event_id, "at": occurred_at, "state": state},
    )
    return event_id


async def add_payload(db: DbConnection, event_id: UUID, created_at: datetime) -> None:
    await db.execute(
        "INSERT INTO web_search_export_payloads (event_id, raw_query, created_at) VALUES (%(id)s, 'raw', %(at)s)",
        {"id": event_id, "at": created_at},
    )


async def event_ids(db: DbConnection) -> set[UUID]:
    return {row["id"] for row in await db.fetchall("SELECT id FROM web_search_events") if isinstance(row["id"], UUID)}


async def payload_ids(db: DbConnection) -> set[UUID]:
    rows = await db.fetchall("SELECT event_id FROM web_search_export_payloads")
    return {row["event_id"] for row in rows if isinstance(row["event_id"], UUID)}


async def test_event_retention_deletes_old_events_in_bounded_oldest_first_batches(
    analytics_db: AnalyticsDatabase,
) -> None:
    # Given five old events in every export state, one exactly at the cutoff and one after it.
    db = analytics_db.db
    states = ["pending", "exporting", "exported", "skipped", "exported"]
    old = [await add_event(db, OLD + timedelta(minutes=minutes), state=state) for minutes, state in enumerate(states)]
    boundary, recent = await add_event(db, CUTOFF), await add_event(db, RECENT)
    # When batches of two are deleted until one comes back short.
    first = await web_search_event_dao.delete_older_than(CUTOFF, limit=2)
    left_after_first = await event_ids(db)
    second = await web_search_event_dao.delete_older_than(CUTOFF, limit=2)
    third = await web_search_event_dao.delete_older_than(CUTOFF, limit=2)
    fourth = await web_search_event_dao.delete_older_than(CUTOFF, limit=2)
    # Then no batch exceeded its limit, the oldest went first, and only events strictly before the cutoff went.
    assert (first, second, third, fourth) == (2, 2, 1, 0)
    assert left_after_first == {*old[2:], boundary, recent}
    assert await event_ids(db) == {boundary, recent}


async def test_payload_retention_deletes_old_payloads_in_bounded_oldest_first_batches(
    analytics_db: AnalyticsDatabase,
) -> None:
    # Given five old payloads of pending events, one at the cutoff and one after it.
    db = analytics_db.db
    events = [await add_event(db, RECENT, state="pending") for _ in range(7)]
    for minutes, event_id in enumerate(events[:5]):
        await add_payload(db, event_id, OLD + timedelta(minutes=minutes))
    await add_payload(db, events[5], CUTOFF)
    await add_payload(db, events[6], RECENT)
    # When batches of two are deleted until one comes back short.
    first = await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=2)
    left_after_first = await payload_ids(db)
    second = await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=2)
    third = await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=2)
    fourth = await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=2)
    # Then the batches were bounded and oldest first, and no event row went with its payload.
    assert (first, second, third, fourth) == (2, 2, 1, 0)
    assert left_after_first == {*events[2:]}
    assert await payload_ids(db) == {events[5], events[6]}
    assert await event_ids(db) == set(events)


async def test_orphaned_payload_retention_keeps_payloads_of_pending_and_exporting_events(
    analytics_db: AnalyticsDatabase,
) -> None:
    # Given payloads of pending and exporting events, and payloads that no export waits for: exported and skipped
    # events and an event that is gone.
    db = analytics_db.db
    waiting = [await add_event(db, RECENT, state="pending"), await add_event(db, RECENT, state="exporting")]
    orphans = [await add_event(db, RECENT, state="exported"), await add_event(db, RECENT, state="skipped"), uuid4()]
    for event_id in [*waiting, *orphans]:
        await add_payload(db, event_id, RECENT)
    # When batches of two are deleted until one comes back short.
    first = await web_search_event_dao.delete_orphaned_payloads(limit=2)
    second = await web_search_event_dao.delete_orphaned_payloads(limit=2)
    third = await web_search_event_dao.delete_orphaned_payloads(limit=2)
    # Then only the orphans went, in bounded batches.
    assert (first, second, third) == (2, 1, 0)
    assert await payload_ids(db) == set(waiting)


async def test_retention_skips_rows_another_transaction_holds_without_waiting(
    analytics_db: AnalyticsDatabase,
) -> None:
    # Given an old event and an old payload, each locked by another open transaction, and a free pair beside them.
    db = analytics_db.db
    held_event, free_event = await add_event(db, OLD), await add_event(db, OLD)
    held_payload, free_payload = await add_event(db, RECENT), await add_event(db, RECENT)
    for event_id in (held_payload, free_payload):
        await add_payload(db, event_id, OLD)
    async with await AsyncConnection[DictRow].connect(analytics_db.dsn, row_factory=dict_row) as other:
        holder = DbConnection(other)
        await holder.execute(f'SET search_path TO "{analytics_db.schema}"')
        await holder.fetchall("SELECT id FROM web_search_events WHERE id = %(id)s FOR UPDATE", {"id": held_event})
        await holder.fetchall(
            "SELECT event_id FROM web_search_export_payloads WHERE event_id = %(id)s FOR UPDATE", {"id": held_payload}
        )
        await db.execute("SET lock_timeout = '1s'")
        # When the purges run meanwhile.
        with anyio.fail_after(FAIL_AFTER_SECONDS):
            events = await web_search_event_dao.delete_older_than(CUTOFF, limit=10)
            payloads = await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=10)
        left_events, left_payloads = await event_ids(db), await payload_ids(db)
        await other.rollback()
        await db.execute("RESET lock_timeout")
    # Then they completed without waiting, took only the free rows, and the held rows went on the next batch.
    assert (events, payloads) == (1, 1)
    assert held_event in left_events
    assert free_event not in left_events
    assert (held_payload in left_payloads, free_payload in left_payloads) == (True, False)
    assert await web_search_event_dao.delete_older_than(CUTOFF, limit=10) == 1
    assert await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=10) == 1


async def test_retention_statements_walk_the_cutoff_indexes_in_small_batches(
    analytics_db: AnalyticsDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the baseline's own indexes on both tables, thousands of old events and payloads, analyzed, and the exact
    # statements the DAO sends (captured).
    db = analytics_db.db
    for statement in _statement_bodies(SCHEMA_BASELINE.read_text()):
        if statement.startswith("CREATE OR REPLACE FUNCTION pg_temp.baseline_ensure_index") or (
            statement.startswith("SELECT pg_temp.baseline_ensure_index")
            and ("ON web_search_events " in statement or "ON web_search_export_payloads " in statement)
        ):
            await db.execute(statement)
    await db.execute(
        "INSERT INTO web_search_events (id, occurred_at, kind, method, http_status, status_class, latency_ms, "
        "query_hash, export_state) SELECT gen_random_uuid(), %(at)s + g * interval '1 minute', 'search', 'POST', 200, "
        "'ok', 5, 'hash', 'exported' FROM generate_series(1, 3000) g",
        {"at": OLD},
    )
    await db.execute(
        "INSERT INTO web_search_export_payloads (event_id, raw_query, created_at) "
        "SELECT gen_random_uuid(), 'raw', %(at)s + g * interval '1 minute' FROM generate_series(1, 3000) g",
        {"at": OLD},
    )
    await db.execute("ANALYZE web_search_events")
    await db.execute("ANALYZE web_search_export_payloads")
    captured: list[tuple[str, Params]] = []
    original = db.fetchall

    async def spy(query: str, params: Params = None) -> list[Row]:
        captured.append((query, params))
        return await original(query, params)

    monkeypatch.setattr(db, "fetchall", spy)
    _ = await web_search_event_dao.delete_older_than(CUTOFF, limit=50)
    _ = await web_search_event_dao.delete_payloads_older_than(CUTOFF, limit=50)
    (events_query, events_params), (payloads_query, payloads_params) = captured[-2:]
    # When the planner is asked about each statement with sequential scans forbidden.
    await db.execute("SET enable_seqscan = off")
    events_plan = "\n".join(
        str(row["QUERY PLAN"]) for row in await original("EXPLAIN (COSTS OFF) " + events_query, events_params)
    )
    payloads_plan = "\n".join(
        str(row["QUERY PLAN"]) for row in await original("EXPLAIN (COSTS OFF) " + payloads_query, payloads_params)
    )
    # Then each reads an index ordered by its cutoff column and deletes by TID.
    assert "ix_web_search_events_occurred_tenant" in events_plan
    assert "ix_web_search_export_payloads_created_at" in payloads_plan
    for plan in (events_plan, payloads_plan):
        assert "Tid Scan" in plan
        assert "Seq Scan" not in plan
