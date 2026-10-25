from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from tests.analytics_response_fixtures import (
    export_status_projection,
    org_ranking_projection,
    summary_projection,
    time_bucket_projection,
    trending_query_projection,
)

from app.api import admin_search_analytics as analytics
from app.config import get_settings
from app.core.domain.users import UserRole
from app.core.json_types import JsonValue
from app.core.security import get_current_user
from app.dao.analytics_projection_contracts import SearchAnalyticsExportStatusProjection
from app.records.user import UserRecord


@pytest.fixture
def analytics_app() -> FastAPI:
    app = FastAPI()
    app.include_router(analytics.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    return app


@pytest.fixture
async def client(analytics_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=analytics_app), base_url="http://test") as session:
        yield session
    analytics_app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("path", "dao_method", "payload", "expected"),
    [
        (
            "summary",
            "summary",
            summary_projection(),
            {**summary_projection(), "scope": "system"},
        ),
        (
            "timeseries",
            "timeseries",
            [time_bucket_projection()],
            [time_bucket_projection()],
        ),
        (
            "orgs",
            "top_orgs",
            [org_ranking_projection()],
            [org_ranking_projection()],
        ),
        (
            "trending",
            "trending",
            [trending_query_projection()],
            [trending_query_projection()],
        ),
    ],
)
async def test_analytics_http_preserves_complete_projection_values(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    dao_method: str,
    payload: JsonValue,
    expected: JsonValue,
) -> None:
    # Given a complete normalized projection, including legitimate nulls.
    async def projection(**kwargs: object) -> JsonValue:
        return payload

    monkeypatch.setattr(analytics.web_search_event_dao, dao_method, projection)
    # When passing through the real HTTP serializer.
    response = await client.get(f"/api/admin/search-analytics/{path}")
    # Then the public values survive without normalization or invented defaults.
    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (None, None),
        ("raw-sql-time", "raw-sql-time"),
        ("2026-10-25T01:02:03+00:00", "2026-10-25T01:02:03+00:00"),
        (datetime(2026, 10, 25, 1, 2, 3, tzinfo=UTC), "2026-10-25T01:02:03Z"),
        (
            datetime(2026, 10, 25, 6, 32, 3, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            "2026-10-25T06:32:03+05:30",
        ),
    ],
)
async def test_export_status_http_preserves_raw_timestamp_and_nullable_config(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    timestamp: datetime | str | None,
    expected: str | None,
) -> None:
    # Given a complete projection at the existing raw SQL-driver timestamp seam.
    async def export_status() -> SearchAnalyticsExportStatusProjection:
        return export_status_projection(timestamp)

    monkeypatch.setattr(analytics.web_search_event_dao, "export_status", export_status)
    monkeypatch.setattr(analytics, "analytics_bucket", lambda: "")
    monkeypatch.setattr(analytics, "analytics_prefix", lambda: "events/")
    monkeypatch.setattr(get_settings(), "WEB_SEARCH_ANALYTICS_EXPORT_ENABLED", False)
    monkeypatch.setattr(get_settings(), "WEB_SEARCH_ANALYTICS_INCLUDE_RAW", False)
    monkeypatch.setattr(get_settings(), "WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED", True)
    # When returning the export status over HTTP.
    response = await client.get("/api/admin/search-analytics/export-status")
    # Then timestamp formatting, complete counts and nullable config stay unchanged.
    assert response.status_code == 200
    assert response.json() == {
        "pending": 1,
        "exporting": 0,
        "exported": 2,
        "skipped": 0,
        "last_exported_at": expected,
        "export_enabled": False,
        "include_raw": False,
        "capture_enabled": True,
        "bucket": None,
        "prefix": "events/",
    }


@pytest.mark.parametrize(
    ("path", "dao_method", "field", "incomplete"),
    [
        (path, method, field, {key: value for key, value in row.items() if key != field})
        for path, method, row in [
            ("summary", "summary", summary_projection()),
            ("timeseries", "timeseries", time_bucket_projection()),
            ("orgs", "top_orgs", org_ranking_projection()),
            ("trending", "trending", trending_query_projection()),
            ("export-status", "export_status", export_status_projection()),
        ]
        for field in row
    ],
)
async def test_analytics_http_rejects_each_missing_projection_field(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    dao_method: str,
    field: str,
    incomplete: dict[str, object],
) -> None:
    # Given an otherwise valid DAO row with one required field omitted.
    async def projection(**kwargs: object) -> object:
        return incomplete if path in {"summary", "export-status"} else [incomplete]

    monkeypatch.setattr(analytics.web_search_event_dao, dao_method, projection)
    # When serializing an incomplete projection through the real response model.
    with pytest.raises(ResponseValidationError) as error:
        await client.get(f"/api/admin/search-analytics/{path}")
    # Then the missing field, including required nullable fields, is rejected.
    location = ("response", field) if path in {"summary", "export-status"} else ("response", 0, field)
    assert [(item["type"], item["loc"]) for item in error.value.errors()] == [("missing", location)]


@pytest.mark.parametrize("field", ["kind", "event_count"])
async def test_analytics_http_rejects_missing_nested_kind_count_fields(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    # Given a complete summary with a malformed nested aggregation row.
    payload: dict[str, object] = dict(summary_projection())
    payload["by_kind"] = [{key: value for key, value in {"kind": "search", "event_count": 2}.items() if key != field}]

    async def summary(**kwargs: object) -> dict[str, object]:
        return payload

    monkeypatch.setattr(analytics.web_search_event_dao, "summary", summary)
    # When serializing the summary.
    with pytest.raises(ResponseValidationError) as error:
        await client.get("/api/admin/search-analytics/summary")
    # Then nested required fields are enforced too.
    assert error.value.errors()[0]["loc"] == ("response", "by_kind", 0, field)


@pytest.mark.parametrize("role", [UserRole.MEMBER, UserRole.ORG_ADMIN])
async def test_analytics_http_keeps_platform_admin_boundary(
    client: httpx.AsyncClient,
    analytics_app: FastAPI,
    role: UserRole,
) -> None:
    # Given an authenticated caller who is not a platform admin.
    analytics_app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role=role)
    # When requesting privileged analytics without any DAO fake.
    response = await client.get("/api/admin/search-analytics/summary")
    # Then authorization rejects the request before persistence is reached.
    assert response.status_code == 403


async def test_analytics_http_rejects_invalid_known_projection_fields(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a malformed known counter from the persistence boundary.
    async def summary(**kwargs: object) -> dict[str, object]:
        return {**summary_projection(), "event_count": "invalid"}

    monkeypatch.setattr(analytics.web_search_event_dao, "summary", summary)
    # When serializing it through the response model.
    with pytest.raises(ResponseValidationError) as error:
        await client.get("/api/admin/search-analytics/summary")
    # Then typed fields are enforced at runtime, not just described in OpenAPI.
    assert error.value.errors()[0]["loc"] == ("response", "event_count")
