"""Platform-admin web search analytics (no org-admin access)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.core.security import require_role
from app.dao.web_search_event_dao import web_search_event_dao
from app.records.user import UserRecord
from app.services.linkup.export import analytics_bucket, analytics_prefix

router = APIRouter(prefix="/admin/search-analytics", tags=["admin"])

_MAX_RANGE = timedelta(days=90)
_DEFAULT_RANGE = timedelta(days=7)


def _window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    resolved_end = end or now
    resolved_start = start or (resolved_end - _DEFAULT_RANGE)
    if resolved_start.tzinfo is None:
        resolved_start = resolved_start.replace(tzinfo=UTC)
    if resolved_end.tzinfo is None:
        resolved_end = resolved_end.replace(tzinfo=UTC)
    if resolved_end <= resolved_start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if resolved_end - resolved_start > _MAX_RANGE:
        raise HTTPException(status_code=400, detail="range cannot exceed 90 days")
    return resolved_start, resolved_end


@router.get("/summary")
async def get_search_analytics_summary(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    tenant_id: UUID | None = None,
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> dict[str, object]:
    del current_user
    window_start, window_end = _window(start, end)
    payload = await web_search_event_dao.summary(start=window_start, end=window_end, tenant_id=tenant_id)
    payload["scope"] = "org" if tenant_id is not None else "system"
    return payload


@router.get("/timeseries")
async def get_search_analytics_timeseries(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    tenant_id: UUID | None = None,
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> list[dict[str, object]]:
    del current_user
    window_start, window_end = _window(start, end)
    return await web_search_event_dao.timeseries(start=window_start, end=window_end, tenant_id=tenant_id)


@router.get("/orgs")
async def get_search_analytics_orgs(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> list[dict[str, object]]:
    del current_user
    window_start, window_end = _window(start, end)
    return await web_search_event_dao.top_orgs(start=window_start, end=window_end, limit=limit)


@router.get("/trending")
async def get_search_analytics_trending(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    tenant_id: UUID | None = None,
    scope: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> list[dict[str, object]]:
    del current_user
    del scope
    window_start, window_end = _window(start, end)
    return await web_search_event_dao.trending(
        start=window_start,
        end=window_end,
        tenant_id=tenant_id,
        limit=limit,
        system_wide=tenant_id is None,
    )


@router.get("/export-status")
async def get_search_analytics_export_status(
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> dict[str, object]:
    del current_user
    settings = get_settings()
    status = await web_search_event_dao.export_status()
    status["export_enabled"] = settings.WEB_SEARCH_ANALYTICS_EXPORT_ENABLED
    status["include_raw"] = settings.WEB_SEARCH_ANALYTICS_INCLUDE_RAW
    status["capture_enabled"] = settings.WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED
    status["bucket"] = analytics_bucket() or None
    status["prefix"] = analytics_prefix()
    return status
