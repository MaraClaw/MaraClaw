from datetime import datetime
from typing import Annotated, Literal, TypedDict

from app.dao.analytics_projection_contracts import (
    SearchAnalyticsExportStatusProjection,
    SearchAnalyticsKindCountProjection,
    SearchAnalyticsOrgRankingProjection,
    SearchAnalyticsSummaryProjection,
    SearchAnalyticsTimeBucketProjection,
    SearchAnalyticsTrendingQueryProjection,
)
from app.schemas.operator_response_contract import response_contract

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
    last_exported_at: datetime | None


type SearchAnalyticsSummaryPassthrough = Annotated[SearchAnalyticsSummary, response_contract(SearchAnalyticsSummary)]
type SearchAnalyticsTimeseriesPassthrough = Annotated[
    list[SearchAnalyticsTimeBucketProjection], response_contract(list[SearchAnalyticsTimeBucket])
]
type SearchAnalyticsOrgsPassthrough = Annotated[
    list[SearchAnalyticsOrgRankingProjection], response_contract(list[SearchAnalyticsOrgRanking])
]
type SearchAnalyticsTrendingPassthrough = Annotated[
    list[SearchAnalyticsTrendingQueryProjection], response_contract(list[SearchAnalyticsTrendingQuery])
]
type SearchAnalyticsExportStatusPassthrough = Annotated[
    SearchAnalyticsExportStatusPayload, response_contract(SearchAnalyticsExportStatus)
]
