from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import assert_type
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeForm

from app.dao.analytics_projection_contracts import (
    PlazaStatsProjection,
    SearchAnalyticsExportStatusProjection,
    SearchAnalyticsOrgRankingProjection,
    SearchAnalyticsSummaryProjection,
    SearchAnalyticsTimeBucketProjection,
    SearchAnalyticsTrendingQueryProjection,
)
from app.dao.plaza_dao import plaza_post_dao
from app.dao.web_search_event_dao import web_search_event_dao
from app.db import pool as pool_module, session as session_module
from app.db.connection import Params, Row

_START = datetime(2026, 10, 20, tzinfo=UTC)
_END = datetime(2026, 10, 21, tzinfo=UTC)


class _Cursor:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw: _RawConnection = raw
        self.rows: list[Row] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Params = None) -> None:
        assert params is None or isinstance(params, Mapping)
        self.raw.executed.append((" ".join(query.split()), dict(params) if params is not None else {}))
        self.rows = self.raw.responses.pop(0)

    async def fetchone(self) -> Row | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[Row]:
        return self.rows


class _RawConnection:
    def __init__(self) -> None:
        self.responses: list[list[Row]] = []
        self.executed: list[tuple[str, dict[str, object]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _PoolConnection:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw: _RawConnection = raw

    async def __aenter__(self) -> _RawConnection:
        return self.raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Pool:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw: _RawConnection = raw

    def connection(self) -> _PoolConnection:
        return _PoolConnection(self.raw)


@pytest.fixture
def raw(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RawConnection]:
    connection = _RawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _Pool(connection))
    token = session_module._conn_ctx.set(None)
    try:
        yield connection
    finally:
        session_module._conn_ctx.reset(token)
        pool_module.reset_pool_for_tests()


@pytest.mark.parametrize("tenant_id", [None, UUID(int=10)])
async def test_summary_normalizes_cells_and_preserves_kind_order(raw: _RawConnection, tenant_id: UUID | None) -> None:
    raw.responses = [
        [
            {
                "event_count": 7.9,
                "error_count": True,
                "quota_count": "3",
                "unique_orgs": 2,
                "unique_agents": 4,
                "unattributed_count": None,
                "avg_latency_ms": Decimal("2.75"),
            }
        ],
        [{"kind": "search", "event_count": 7}, {"kind": None, "event_count": 2.9}],
    ]

    result = await web_search_event_dao.summary(start=_START, end=_END, tenant_id=tenant_id)

    assert_type(result, SearchAnalyticsSummaryProjection)
    assert result == {
        "event_count": 7,
        "error_count": 0,
        "quota_count": 0,
        "unique_orgs": 2,
        "unique_agents": 4,
        "unattributed_count": 0,
        "avg_latency_ms": 2.75,
        "by_kind": [{"kind": "search", "event_count": 7}, {"kind": "", "event_count": 2}],
    }
    adapter = TypeAdapter(SearchAnalyticsSummaryProjection)
    assert adapter.validate_json(adapter.dump_json(result), strict=True) == result
    assert [params for _query, params in raw.executed] == [{"start": _START, "end": _END, "tenant_id": tenant_id}] * 2
    assert raw.executed[1][0].endswith("GROUP BY kind ORDER BY kind")


@pytest.mark.parametrize("totals", [[], [{}]])
async def test_summary_keeps_missing_totals_defaults(raw: _RawConnection, totals: list[Row]) -> None:
    raw.responses = [totals, []]

    result = await web_search_event_dao.summary(start=_START, end=_END, tenant_id=None)

    assert result == {
        "event_count": 0,
        "error_count": 0,
        "quota_count": 0,
        "unique_orgs": 0,
        "unique_agents": 0,
        "unattributed_count": 0,
        "avg_latency_ms": 0.0,
        "by_kind": [],
    }
    assert isinstance(result["avg_latency_ms"], float)


@pytest.mark.parametrize("rows", [[], [{"pending": 3.9, "exporting": True, "exported": "2", "skipped": None}]])
async def test_export_status_keeps_counter_defaults(raw: _RawConnection, rows: list[Row]) -> None:
    raw.responses = [rows]

    result = await web_search_event_dao.export_status()

    assert_type(result, SearchAnalyticsExportStatusProjection)
    assert result == {
        "pending": 3 if rows else 0,
        "exporting": 0,
        "exported": 0,
        "skipped": 0,
        "last_exported_at": None,
    }
    adapter = TypeAdapter(SearchAnalyticsExportStatusProjection)
    assert adapter.validate_json(adapter.dump_json(result), strict=True) == result
    assert raw.executed[0][1] == {}


@pytest.mark.parametrize(
    "exported_at", [None, datetime(2026, 10, 20, 1, 2, 3, tzinfo=timezone(timedelta(hours=5, minutes=30))), "raw-time"]
)
async def test_export_status_preserves_raw_timestamp(raw: _RawConnection, exported_at: object) -> None:
    raw.responses = [[{"pending": 1, "last_exported_at": exported_at}]]

    result = await web_search_event_dao.export_status()

    assert result["last_exported_at"] is exported_at
    adapter = TypeAdapter(SearchAnalyticsExportStatusProjection)
    assert adapter.validate_python(result, strict=True) == result
    wire = adapter.validate_json(adapter.dump_json(result))
    assert wire["last_exported_at"] == (exported_at.isoformat() if isinstance(exported_at, datetime) else exported_at)


@pytest.mark.parametrize("empty", [False, True])
async def test_timeseries_keeps_date_stringification_and_order(raw: _RawConnection, empty: bool) -> None:
    raw.responses = [[] if empty else [{"bucket_date": date(2026, 10, 20), "event_count": 2.9}, {"bucket_date": None}]]

    result = await web_search_event_dao.timeseries(start=_START, end=_END, tenant_id=None)

    assert_type(result, list[SearchAnalyticsTimeBucketProjection])
    assert result == (
        []
        if empty
        else [
            {"date": "2026-10-20", "event_count": 2, "error_count": 0, "quota_count": 0},
            {"date": "None", "event_count": 0, "error_count": 0, "quota_count": 0},
        ]
    )
    adapter = TypeAdapter(list[SearchAnalyticsTimeBucketProjection])
    assert adapter.validate_json(adapter.dump_json(result), strict=True) == result
    assert "GROUP BY 1 ORDER BY 1" in raw.executed[0][0]
    assert raw.executed[0][1] == {"start": _START, "end": _END, "tenant_id": None}


@pytest.mark.parametrize("empty", [False, True])
async def test_org_ranking_keeps_uuid_strings_nulls_and_name_fallback(raw: _RawConnection, empty: bool) -> None:
    tenant_id = UUID(int=11)
    raw.responses = [
        []
        if empty
        else [
            {"tenant_id": tenant_id, "tenant_name": "", "event_count": 4.5},
            {"tenant_id": None, "tenant_name": 42, "quota_count": 1},
        ]
    ]

    result = await web_search_event_dao.top_orgs(start=_START, end=_END, limit=8)

    assert_type(result, list[SearchAnalyticsOrgRankingProjection])
    assert result == (
        []
        if empty
        else [
            {"tenant_id": str(tenant_id), "name": "Unknown org", "event_count": 4, "quota_count": 0},
            {"tenant_id": None, "name": "42", "event_count": 0, "quota_count": 1},
        ]
    )
    adapter = TypeAdapter(list[SearchAnalyticsOrgRankingProjection])
    assert adapter.validate_json(adapter.dump_json(result), strict=True) == result
    assert raw.executed[0][0].endswith("ORDER BY event_count DESC LIMIT %(limit)s")
    assert raw.executed[0][1] == {"start": _START, "end": _END, "limit": 8}


@pytest.mark.parametrize("system_wide", [False, True])
@pytest.mark.parametrize("empty", [False, True])
async def test_trending_keeps_normalization_and_scope_order(
    raw: _RawConnection, system_wide: bool, empty: bool
) -> None:
    tenant_id = UUID(int=12)
    raw.responses = [
        []
        if empty
        else [
            {
                "query_hash": 7,
                "query_normalized": "hello",
                "kind": None,
                "hits": 5.9,
                "distinct_agents": True,
                "distinct_orgs": 2,
            }
        ]
    ]

    result = await web_search_event_dao.trending(
        start=_START, end=_END, tenant_id=tenant_id, limit=9, system_wide=system_wide
    )

    assert_type(result, list[SearchAnalyticsTrendingQueryProjection])
    assert result == (
        []
        if empty
        else [
            {
                "query_hash": "7",
                "query_normalized": "hello",
                "kind": "",
                "hits": 5,
                "distinct_agents": 0,
                "distinct_orgs": 2,
            }
        ]
    )
    adapter = TypeAdapter(list[SearchAnalyticsTrendingQueryProjection])
    assert adapter.validate_json(adapter.dump_json(result), strict=True) == result
    order = "distinct_orgs DESC, hits DESC" if system_wide else "hits DESC"
    assert raw.executed[0][0].endswith(f"ORDER BY {order} LIMIT %(limit)s")
    assert raw.executed[0][1] == {"start": _START, "end": _END, "tenant_id": tenant_id, "limit": 9}


@pytest.mark.parametrize("tenant_id", [None, UUID(int=13), "tenant-'quoted"])
async def test_plaza_stats_keep_raw_labels_and_normalize_counts(
    raw: _RawConnection, tenant_id: UUID | str | None
) -> None:
    label = {"raw": None}
    raw.responses = [
        [{"count": 3.9}],
        [{"count": True}],
        [],
        [
            {"author_name": label, "author_type": None, "post_count": 2.9},
            {"author_name": "A", "author_type": "human", "post_count": "5"},
        ],
    ]

    result = await plaza_post_dao.get_stats(tenant_id)

    assert_type(result, PlazaStatsProjection)
    assert result == {
        "total_posts": 3,
        "total_comments": 0,
        "today_posts": 0,
        "top_contributors": [
            {"name": {"raw": None}, "type": None, "posts": 2},
            {"name": "A", "type": "human", "posts": 0},
        ],
    }
    assert result["top_contributors"][0]["name"] is label
    adapter = TypeAdapter(PlazaStatsProjection)
    assert adapter.validate_json(adapter.dump_json(result), strict=True) == result
    assert [params for _query, params in raw.executed] == (
        [{}] if tenant_id is None else [{"tenant_id": tenant_id}]
    ) * 4
    assert all("a.is_system IS TRUE" in query and "a.access_mode" in query for query, _params in raw.executed)
    assert raw.executed[-1][0].endswith("GROUP BY author_name, author_type ORDER BY post_count DESC LIMIT 5")


async def test_plaza_stats_keep_empty_defaults(raw: _RawConnection) -> None:
    raw.responses = [[], [], [], []]

    result = await plaza_post_dao.get_stats()

    assert result == {"total_posts": 0, "total_comments": 0, "today_posts": 0, "top_contributors": []}


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (SearchAnalyticsSummaryProjection, {"event_count": 1, "by_kind": []}),
        (SearchAnalyticsTimeBucketProjection, {"date": None, "event_count": 1, "error_count": 0, "quota_count": 0}),
        (
            SearchAnalyticsOrgRankingProjection,
            {"tenant_id": None, "name": "Org", "event_count": "bad", "quota_count": 0},
        ),
        (SearchAnalyticsTrendingQueryProjection, {"query_hash": "hash", "hits": 1}),
        (SearchAnalyticsExportStatusProjection, {"pending": 0, "exporting": 0, "exported": 0, "skipped": 0}),
        (
            PlazaStatsProjection,
            {
                "total_posts": 0,
                "total_comments": 0,
                "today_posts": 0,
                "top_contributors": [{"name": "A", "type": "human"}],
            },
        ),
    ],
)
def test_projection_contracts_reject_missing_or_invalid_normalized_fields(
    contract: TypeForm[object], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter[object](contract).validate_python(payload, strict=True)
