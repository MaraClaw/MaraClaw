"""Aggregate projections; unnormalized SQL cells remain explicitly opaque."""

from typing import TypedDict


class SearchAnalyticsKindCountProjection(TypedDict):
    kind: str
    event_count: int


class SearchAnalyticsSummaryProjection(TypedDict):
    event_count: int
    error_count: int
    quota_count: int
    unique_orgs: int
    unique_agents: int
    unattributed_count: int
    avg_latency_ms: float
    by_kind: list[SearchAnalyticsKindCountProjection]


class SearchAnalyticsTimeBucketProjection(TypedDict):
    date: str
    event_count: int
    error_count: int
    quota_count: int


class SearchAnalyticsOrgRankingProjection(TypedDict):
    tenant_id: str | None
    name: str
    event_count: int
    quota_count: int


class SearchAnalyticsTrendingQueryProjection(TypedDict):
    query_hash: str
    query_normalized: str
    kind: str
    hits: int
    distinct_agents: int
    distinct_orgs: int


class SearchAnalyticsExportStatusProjection(TypedDict):
    """Counters are normalized; the timestamp retains its SQL-driver value."""

    pending: int
    exporting: int
    exported: int
    skipped: int
    last_exported_at: object


class PlazaContributorProjection(TypedDict):
    """Only the post count is normalized, not the SQL-selected author labels."""

    name: object
    type: object
    posts: int


class PlazaStatsProjection(TypedDict):
    total_posts: int
    total_comments: int
    today_posts: int
    top_contributors: list[PlazaContributorProjection]
