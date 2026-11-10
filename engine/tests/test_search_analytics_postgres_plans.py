import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from tests.search_analytics_postgres_fixtures import END, START, TENANT, AnalyticsDatabase, analytics_db

from app.core.json_types import JsonObject, is_json_value, json_object_from
from app.dao.web_search_analytics_sql import DASHBOARD, KINDS, ORGS, SUMMARY, TIMESERIES, TRENDING
from app.db.connection import DbConnection

__all__ = ["analytics_db"]


def event_scans(plan: JsonObject) -> Iterator[JsonObject]:
    if plan.get("Relation Name") == "web_search_events":
        yield plan
    children = plan.get("Plans", [])
    assert isinstance(children, list)
    for child in children:
        yield from event_scans(json_object_from(child))


async def explain(db: DbConnection, query: str, params: dict[str, object]) -> JsonObject:
    result = await db.fetchval("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params)
    assert is_json_value(result)
    assert isinstance(result, list)
    return json_object_from(result[0])


def plan_metrics(plans: list[JsonObject]) -> dict[str, float]:
    result = {"event_rows": 0.0, "buffers": 0.0, "temp_read": 0.0, "temp_written": 0.0, "execution_ms": 0.0}
    for document in plans:
        root = json_object_from(document["Plan"])
        elapsed = document["Execution Time"]
        assert isinstance(elapsed, (int, float))
        result["execution_ms"] += elapsed
        for key, source in (
            ("buffers", "Shared Hit Blocks"),
            ("buffers", "Shared Read Blocks"),
            ("temp_read", "Temp Read Blocks"),
            ("temp_written", "Temp Written Blocks"),
        ):
            count = root.get(source, 0)
            assert isinstance(count, (int, float))
            result[key] += count
        for scan in event_scans(root):
            rows, filtered, loops = scan["Actual Rows"], scan.get("Rows Removed by Filter", 0), scan["Actual Loops"]
            assert isinstance(rows, (int, float))
            assert isinstance(filtered, (int, float))
            assert isinstance(loops, (int, float))
            result["event_rows"] += (rows + filtered) * loops
    return result


@pytest.mark.parametrize(
    ("scenario", "tenant_id", "diversity"),
    [("system", None, 40), ("small-tenant-large-window", TENANT, 40), ("high-diversity", None, 30000)],
)
async def test_dashboard_postgres_plan_comparison(
    analytics_db: AnalyticsDatabase, scenario: str, tenant_id: UUID | None, diversity: int, tmp_path: Path
) -> None:
    db = analytics_db.db
    await db.execute(
        "INSERT INTO web_search_events (id,occurred_at,tenant_id,agent_id,kind,method,http_status,status_class,"
        "latency_ms,query_hash,query_normalized) SELECT md5(i::text)::uuid, "
        "%(start)s::timestamptz + (i %% 604799) * interval '1 second', "
        "CASE WHEN i <= 7 THEN %(tenant)s::uuid ELSE md5((i %% 100 + 100)::text)::uuid END, "
        "md5((i %% 500)::text)::uuid, CASE WHEN i %% 10 = 0 THEN 'crawl' ELSE 'search' END, 'POST', 200, "
        "CASE WHEN i %% 13 = 0 THEN 'quota' ELSE 'ok' END, i %% 3000, "
        "md5((i %% %(diversity)s)::text), (i %% %(diversity)s)::text FROM generate_series(1, 30000) i",
        {"start": START, "tenant": TENANT, "diversity": diversity},
    )
    await db.execute("ANALYZE web_search_events")
    await db.execute("SET work_mem = '1MB'")
    selected_params: dict[str, object] = {"start": START, "end": END, "tenant_id": tenant_id}
    global_params: dict[str, object] = {**selected_params, "tenant_id": None}
    legacy = [await explain(db, SUMMARY, global_params), await explain(db, KINDS, global_params)]
    if tenant_id is not None:
        legacy += [await explain(db, SUMMARY, selected_params), await explain(db, KINDS, selected_params)]
    legacy += [
        await explain(db, TIMESERIES, selected_params),
        await explain(db, ORGS, {"start": START, "end": END, "limit": 20}),
        await explain(
            db,
            TRENDING
            + ("ORDER BY distinct_orgs DESC, hits DESC " if tenant_id is None else "ORDER BY hits DESC ")
            + "LIMIT %(limit)s",
            {**selected_params, "limit": 50},
        ),
    ]
    combined = await explain(db, DASHBOARD, selected_params)
    old_metrics = plan_metrics(legacy)
    new_metrics = plan_metrics([combined])
    evidence = {"scenario": scenario, "legacy": old_metrics, "dashboard": new_metrics}
    (tmp_path / "plan-evidence.json").write_text(
        json.dumps({"metrics": evidence, "legacy_plans": legacy, "dashboard_plan": combined}, indent=2, sort_keys=True)
    )
    assert new_metrics["event_rows"] < old_metrics["event_rows"], evidence
    assert len(list(event_scans(json_object_from(combined["Plan"])))) == 4
