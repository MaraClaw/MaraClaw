from app.dao.analytics_projection_contracts import (
    SearchAnalyticsExportStatusProjection,
    SearchAnalyticsOrgRankingProjection,
    SearchAnalyticsSummaryProjection,
    SearchAnalyticsTimeBucketProjection,
    SearchAnalyticsTrendingQueryProjection,
)


def summary_projection() -> SearchAnalyticsSummaryProjection:
    return {
        "event_count": 2,
        "error_count": 0,
        "quota_count": 1,
        "unique_orgs": 1,
        "unique_agents": 1,
        "unattributed_count": 0,
        "avg_latency_ms": 2.5,
        "by_kind": [{"kind": "search", "event_count": 2}],
    }


def time_bucket_projection() -> SearchAnalyticsTimeBucketProjection:
    return {"date": "2026-10-25", "event_count": 2, "error_count": 0, "quota_count": 1}


def org_ranking_projection() -> SearchAnalyticsOrgRankingProjection:
    return {"tenant_id": None, "name": "Unknown org", "event_count": 2, "quota_count": 1}


def trending_query_projection() -> SearchAnalyticsTrendingQueryProjection:
    return {
        "query_hash": "hash",
        "query_normalized": "stripe ceo",
        "kind": "search",
        "hits": 2,
        "distinct_agents": 1,
        "distinct_orgs": 1,
    }


def export_status_projection(timestamp: object = None) -> SearchAnalyticsExportStatusProjection:
    return {"pending": 1, "exporting": 0, "exported": 2, "skipped": 0, "last_exported_at": timestamp}
