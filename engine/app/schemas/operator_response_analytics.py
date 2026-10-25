from datetime import datetime
from typing import Literal, TypedDict

from app.dao.analytics_projection_contracts import (
    SearchAnalyticsExportStatusProjection,
    SearchAnalyticsKindCountProjection,
    SearchAnalyticsOrgRankingProjection,
    SearchAnalyticsSummaryProjection,
    SearchAnalyticsTimeBucketProjection,
    SearchAnalyticsTrendingQueryProjection,
)

type SearchAnalyticsScope = Literal["org", "system"]


type SearchAnalyticsKindCount = SearchAnalyticsKindCountProjection


class SearchAnalyticsSummaryScope(TypedDict):
    scope: SearchAnalyticsScope


class SearchAnalyticsSummary(SearchAnalyticsSummaryProjection, SearchAnalyticsSummaryScope):
    pass


type SearchAnalyticsTimeBucket = SearchAnalyticsTimeBucketProjection


type SearchAnalyticsOrgRanking = SearchAnalyticsOrgRankingProjection


type SearchAnalyticsTrendingQuery = SearchAnalyticsTrendingQueryProjection


class SearchAnalyticsExportConfig(TypedDict):
    export_enabled: bool
    include_raw: bool
    capture_enabled: bool
    bucket: str | None
    prefix: str


class SearchAnalyticsExportStatusPayload(SearchAnalyticsExportStatusProjection, SearchAnalyticsExportConfig):
    pass


class SearchAnalyticsExportStatus(SearchAnalyticsExportConfig):
    pending: int
    exporting: int
    exported: int
    skipped: int
    last_exported_at: datetime | str | None


type SearchAnalyticsSummaryPassthrough = SearchAnalyticsSummary
type SearchAnalyticsTimeseriesPassthrough = list[SearchAnalyticsTimeBucket]
type SearchAnalyticsOrgsPassthrough = list[SearchAnalyticsOrgRanking]
type SearchAnalyticsTrendingPassthrough = list[SearchAnalyticsTrendingQuery]
type SearchAnalyticsExportStatusPassthrough = SearchAnalyticsExportStatusPayload
