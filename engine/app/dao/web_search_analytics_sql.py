"""Aggregate-only web search projections and their parameterized SQL."""

from typing import TypedDict

from pydantic import TypeAdapter

from app.dao.analytics_projection_contracts import (
    SearchAnalyticsOrgRankingProjection,
    SearchAnalyticsSummaryProjection,
    SearchAnalyticsTimeBucketProjection,
    SearchAnalyticsTrendingQueryProjection,
)


class SearchAnalyticsDashboardProjection(TypedDict):
    system_summary: SearchAnalyticsSummaryProjection
    selected_summary: SearchAnalyticsSummaryProjection
    timeseries: list[SearchAnalyticsTimeBucketProjection]
    orgs: list[SearchAnalyticsOrgRankingProjection]
    trending: list[SearchAnalyticsTrendingQueryProjection]


DASHBOARD_ADAPTER = TypeAdapter(SearchAnalyticsDashboardProjection)

RANGE = (
    "occurred_at >= %(start)s AND occurred_at < %(end)s AND (%(tenant_id)s::uuid IS NULL OR tenant_id = %(tenant_id)s)"
)
SUMMARY = (
    "SELECT count(*) AS event_count, "
    "count(*) FILTER (WHERE status_class NOT IN ('ok', 'quota')) AS error_count, "
    "count(*) FILTER (WHERE status_class = 'quota' OR error_class = 'quota_rotated') AS quota_count, "
    "count(DISTINCT tenant_id) AS unique_orgs, count(DISTINCT agent_id) AS unique_agents, "
    "count(*) FILTER (WHERE tenant_id IS NULL) AS unattributed_count, "
    "COALESCE(avg(latency_ms), 0)::float8 AS avg_latency_ms "
    f"FROM web_search_events WHERE {RANGE}"
)
KINDS = f"SELECT kind, count(*) AS event_count FROM web_search_events WHERE {RANGE} GROUP BY kind ORDER BY kind"
TIMESERIES = (
    "SELECT (occurred_at AT TIME ZONE 'UTC')::date AS bucket_date, count(*) AS event_count, "
    "count(*) FILTER (WHERE status_class NOT IN ('ok', 'quota')) AS error_count, "
    "count(*) FILTER (WHERE status_class = 'quota' OR error_class = 'quota_rotated') AS quota_count "
    f"FROM web_search_events WHERE {RANGE} GROUP BY 1 ORDER BY 1"
)
ORGS = (
    "SELECT e.tenant_id, t.name AS tenant_name, count(*) AS event_count, "
    "count(*) FILTER (WHERE e.status_class = 'quota' OR e.error_class = 'quota_rotated') AS quota_count "
    "FROM web_search_events e LEFT JOIN tenants t ON t.id = e.tenant_id "
    "WHERE e.occurred_at >= %(start)s AND e.occurred_at < %(end)s AND e.tenant_id IS NOT NULL "
    "GROUP BY e.tenant_id, t.name ORDER BY event_count DESC LIMIT %(limit)s"
)
TRENDING = (
    "SELECT query_hash, query_normalized, kind, count(*) AS hits, "
    "count(DISTINCT agent_id) AS distinct_agents, count(DISTINCT tenant_id) AS distinct_orgs "
    f"FROM web_search_events WHERE {RANGE} AND query_normalized <> '' "
    "GROUP BY query_hash, query_normalized, kind "
)

# Only grouped rows are shared. Event ranges remain available to the planner for
# tenant-selective access; no event-sized CTE is forced into a materialized spool.
DASHBOARD = """
WITH summary_groups AS (
    SELECT kind, GROUPING(kind) AS total,
        count(*) AS event_count,
        count(*) FILTER (WHERE status_class NOT IN ('ok', 'quota')) AS error_count,
        count(*) FILTER (WHERE status_class = 'quota' OR error_class = 'quota_rotated') AS quota_count,
        count(DISTINCT tenant_id) AS unique_orgs,
        count(DISTINCT agent_id) AS unique_agents,
        count(*) FILTER (WHERE tenant_id IS NULL) AS unattributed_count,
        COALESCE(avg(latency_ms), 0)::float8 AS avg_latency_ms,
        count(*) FILTER (WHERE tenant_id = %(tenant_id)s) AS selected_count,
        count(*) FILTER (WHERE tenant_id = %(tenant_id)s
            AND status_class NOT IN ('ok', 'quota')) AS selected_errors,
        count(*) FILTER (WHERE tenant_id = %(tenant_id)s
            AND (status_class = 'quota' OR error_class = 'quota_rotated')) AS selected_quotas,
        count(DISTINCT tenant_id) FILTER (WHERE tenant_id = %(tenant_id)s) AS selected_orgs,
        count(DISTINCT agent_id) FILTER (WHERE tenant_id = %(tenant_id)s) AS selected_agents,
        COALESCE(avg(latency_ms) FILTER (WHERE tenant_id = %(tenant_id)s), 0)::float8 AS selected_latency
    FROM web_search_events
    WHERE occurred_at >= %(start)s AND occurred_at < %(end)s
    GROUP BY GROUPING SETS ((), (kind))
), summaries AS (
    SELECT jsonb_build_object(
        'event_count', event_count, 'error_count', error_count, 'quota_count', quota_count,
        'unique_orgs', unique_orgs, 'unique_agents', unique_agents,
        'unattributed_count', unattributed_count, 'avg_latency_ms', avg_latency_ms,
        'by_kind', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'kind', kind, 'event_count', event_count) ORDER BY kind), '[]'::jsonb)
            FROM summary_groups WHERE total = 0)
    ) AS system_summary,
    jsonb_build_object(
        'event_count', selected_count, 'error_count', selected_errors, 'quota_count', selected_quotas,
        'unique_orgs', selected_orgs, 'unique_agents', selected_agents,
        'unattributed_count', 0, 'avg_latency_ms', selected_latency,
        'by_kind', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'kind', kind, 'event_count', selected_count) ORDER BY kind), '[]'::jsonb)
            FROM summary_groups WHERE total = 0 AND selected_count > 0)
    ) AS selected_summary
    FROM summary_groups WHERE total = 1
), daily AS (
    SELECT (occurred_at AT TIME ZONE 'UTC')::date::text AS date,
        count(*) AS event_count,
        count(*) FILTER (WHERE status_class NOT IN ('ok', 'quota')) AS error_count,
        count(*) FILTER (WHERE status_class = 'quota' OR error_class = 'quota_rotated') AS quota_count
    FROM web_search_events
    WHERE occurred_at >= %(start)s AND occurred_at < %(end)s
        AND (%(tenant_id)s::uuid IS NULL OR tenant_id = %(tenant_id)s)
    GROUP BY 1
), organizations AS (
    SELECT e.tenant_id, COALESCE(NULLIF(t.name, ''), 'Unknown org') AS name,
        count(*) AS event_count,
        count(*) FILTER (WHERE e.status_class = 'quota' OR e.error_class = 'quota_rotated') AS quota_count
    FROM web_search_events e LEFT JOIN tenants t ON t.id = e.tenant_id
    WHERE e.occurred_at >= %(start)s AND e.occurred_at < %(end)s AND e.tenant_id IS NOT NULL
    GROUP BY e.tenant_id, t.name
    ORDER BY event_count DESC LIMIT 20
), trends AS (
    SELECT query_hash, query_normalized, kind, count(*) AS hits,
        count(DISTINCT agent_id) AS distinct_agents, count(DISTINCT tenant_id) AS distinct_orgs
    FROM web_search_events
    WHERE occurred_at >= %(start)s AND occurred_at < %(end)s
        AND (%(tenant_id)s::uuid IS NULL OR tenant_id = %(tenant_id)s)
        AND query_normalized <> ''
    GROUP BY query_hash, query_normalized, kind
    ORDER BY CASE WHEN %(tenant_id)s::uuid IS NULL THEN count(DISTINCT tenant_id) END DESC,
        hits DESC
    LIMIT 50
)
SELECT jsonb_build_object(
    'system_summary', system_summary,
    'selected_summary', CASE WHEN %(tenant_id)s::uuid IS NULL THEN system_summary ELSE selected_summary END,
    'timeseries', (SELECT COALESCE(jsonb_agg(to_jsonb(daily) ORDER BY date), '[]'::jsonb) FROM daily),
    'orgs', (SELECT COALESCE(jsonb_agg(to_jsonb(organizations) ORDER BY event_count DESC), '[]'::jsonb)
        FROM organizations),
    'trending', (SELECT COALESCE(jsonb_agg(to_jsonb(trends) ORDER BY
        CASE WHEN %(tenant_id)s::uuid IS NULL THEN distinct_orgs END DESC, hits DESC), '[]'::jsonb)
        FROM trends)
)
FROM summaries
"""
