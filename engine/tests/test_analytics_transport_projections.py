from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter
from tests.analytics_response_fixtures import export_status_projection

from app.api import admin_search_analytics, plaza
from app.api.auth import get_current_user
from app.core.json_types import JsonObject
from app.dao.analytics_projection_contracts import SearchAnalyticsExportStatusProjection
from app.records.user import UserRecord


@pytest.mark.parametrize("exported_at", [None, datetime(2026, 10, 20, tzinfo=UTC), "raw-sql-time"])
async def test_export_status_keeps_complete_projection_identity_and_raw_timestamp(
    monkeypatch: pytest.MonkeyPatch, exported_at: object
) -> None:
    payload = export_status_projection(exported_at)

    async def status() -> SearchAnalyticsExportStatusProjection:
        return payload

    monkeypatch.setattr(admin_search_analytics.web_search_event_dao, "export_status", status)
    monkeypatch.setattr(admin_search_analytics, "analytics_bucket", lambda: "")
    monkeypatch.setattr(admin_search_analytics, "analytics_prefix", lambda: "web-search")

    result = await admin_search_analytics.get_search_analytics_export_status(current_user=UserRecord(id=uuid4()))

    assert result is payload
    assert result["last_exported_at"] is exported_at
    assert result["exported"] == 2
    assert result["bucket"] is None
    assert result["prefix"] == "web-search"


async def test_plaza_stats_transport_keeps_opaque_contributor_labels_and_extensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, object] = {
        "total_posts": 1,
        "total_comments": 2,
        "today_posts": 1,
        "top_contributors": [{"name": {"raw": None}, "type": None, "posts": 1, "metadata": {"extra": True}}],
    }

    async def stats(*, tenant_id: str | None) -> dict[str, object]:
        del tenant_id
        return payload

    monkeypatch.setattr(plaza.plaza_post_dao, "get_stats", stats)
    app = FastAPI()
    app.include_router(plaza.router)
    actor = UserRecord(id=uuid4(), tenant_id=uuid4())
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: actor)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/plaza/stats")

    assert response.status_code == 200
    assert TypeAdapter(JsonObject).validate_json(response.content) == payload
