"""Platform-admin search analytics handlers (no live PG)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.admin_search_analytics import (
    get_search_analytics_export_status,
    get_search_analytics_summary,
    get_search_analytics_trending,
    router,
)


def _pa() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), role="platform_admin", email="pa@example.test")


@pytest.mark.asyncio
async def test_summary_and_trending_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import admin_search_analytics as api

    calls: dict[str, object] = {}

    class FakeDao:
        async def summary(self, **kwargs):
            calls["summary"] = kwargs
            return {"event_count": 2, "by_kind": []}

        async def trending(self, **kwargs):
            calls["trending"] = kwargs
            return [{"query_normalized": "stripe ceo", "hits": 2}]

        async def export_status(self):
            return {"pending": 1, "exporting": 0, "exported": 0, "skipped": 3, "last_exported_at": None}

    monkeypatch.setattr(api, "web_search_event_dao", FakeDao())
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            WEB_SEARCH_ANALYTICS_EXPORT_ENABLED=False,
            WEB_SEARCH_ANALYTICS_INCLUDE_RAW=False,
            WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED=True,
        ),
    )
    monkeypatch.setattr(api, "analytics_bucket", lambda: "")
    monkeypatch.setattr(api, "analytics_prefix", lambda: "web-search")

    summary = await get_search_analytics_summary(
        start=None, end=None, tenant_id=None, current_user=_pa()
    )
    assert summary["event_count"] == 2
    assert summary["scope"] == "system"

    trending = await get_search_analytics_trending(
        start=None, end=None, tenant_id=None, scope=None, current_user=_pa()
    )
    assert trending[0]["query_normalized"] == "stripe ceo"
    assert "query_raw" not in trending[0]
    assert calls["trending"]["system_wide"] is True
    assert calls["trending"]["tenant_id"] is None

    tenant = uuid4()
    org_summary = await get_search_analytics_summary(
        start=None, end=None, tenant_id=tenant, current_user=_pa()
    )
    assert org_summary["scope"] == "org"
    _ = await get_search_analytics_trending(
        start=None, end=None, tenant_id=tenant, scope=None, current_user=_pa()
    )
    assert calls["trending"]["tenant_id"] == tenant
    assert calls["trending"]["system_wide"] is False

    too_long_start = datetime.now(UTC) - timedelta(days=120)
    with pytest.raises(HTTPException) as too_long:
        await get_search_analytics_summary(
            start=too_long_start, end=None, tenant_id=None, current_user=_pa()
        )
    assert too_long.value.status_code == 400

    status = await get_search_analytics_export_status(current_user=_pa())
    assert status["capture_enabled"] is True
    assert status["export_enabled"] is False
    assert status["prefix"] == "web-search"


def test_routes_require_platform_admin() -> None:
    paths = {getattr(route, "path", "") for route in router.routes}
    assert "/admin/search-analytics/summary" in paths
    assert "/admin/search-analytics/trending" in paths
    assert "/admin/search-analytics/export-status" in paths


@pytest.mark.asyncio
async def test_export_path_and_raw_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.records.web_search_event import WebSearchEventRecord
    from app.services.linkup import export as export_mod

    event = WebSearchEventRecord(
        id=uuid4(),
        kind="search",
        method="POST",
        http_status=200,
        status_class="ok",
        latency_ms=9,
        query_hash="abc",
        query_normalized="hello",
        export_state="pending",
        occurred_at=datetime.now(UTC),
    )

    class FakeDao:
        def __init__(self) -> None:
            self.exported: list = []
            self.released: list = []

        async def claim_pending_export(self, **_kwargs):
            return [event]

        async def payloads_for(self, event_ids):
            return {event_ids[0]: "Hello There"}

        async def mark_exported(self, ids, *, now):
            del now
            self.exported.extend(ids)

        async def release_export(self, ids):
            self.released.extend(ids)

        async def delete_payloads(self, ids):
            del ids

    store = FakeDao()
    puts: list[tuple[str, str, bytes]] = []

    class FakeSink:
        async def put(self, *, bucket: str, key: str, body: bytes, content_type: str) -> None:
            del content_type
            puts.append((bucket, key, body))

    monkeypatch.setattr(export_mod, "web_search_event_dao", store)
    monkeypatch.setattr(
        export_mod,
        "get_settings",
        lambda: SimpleNamespace(
            WEB_SEARCH_ANALYTICS_EXPORT_ENABLED=True,
            WEB_SEARCH_ANALYTICS_INCLUDE_RAW=True,
            ANALYTICS_S3_BUCKET="lake",
            ANALYTICS_S3_PREFIX="web-search/",
            S3_BUCKET="",
            INSTANCE_ID="test-node",
        ),
    )

    count = await export_mod.drain_pending_exports(sink=FakeSink())
    assert count == 1
    assert store.exported == [event.id]
    bucket, key, body = puts[0]
    assert bucket == "lake"
    assert key.startswith("web-search/dt=")
    assert "/hour=" in key
    assert "agents/" not in key
    import gzip

    line = gzip.decompress(body).decode("utf-8")
    assert '"query_raw":"Hello There"' in line
    assert '"query_normalized":"hello"' in line
    assert store.released == []
