"""Platform-admin agent resource-pressure episodes: per-organization summary, episode pages and CSV (no org-admin access)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.security import require_role
from app.dao.agent_resource_pressure_episode_dao import EpisodeFilters
from app.records.agent_resource_pressure_episode import PressureKind, PressureResource
from app.records.organization_plan import OrganizationPlanId
from app.records.user import UserRecord
from app.schemas.agent_resource_pressure import ResourcePressureEpisodePage, ResourcePressureTenantSummary
from app.services.agent_resource_pressure_report import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    episode_page,
    stream_episodes_csv,
    tenant_summaries,
)

router = APIRouter(prefix="/admin/resource-pressure", tags=["admin"])
export_router = APIRouter(prefix="/admin/resource-pressure", tags=["admin"])


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _filters(
    since: datetime | None,
    until: datetime | None,
    tier: OrganizationPlanId | None,
    resource: PressureResource | None,
    tenant_id: UUID | None,
    agent_id: UUID | None = None,
    kind: PressureKind | None = None,
    open_only: bool = False,
) -> EpisodeFilters:
    start, end = _aware(since), _aware(until)
    if start is not None and end is not None and end <= start:
        raise HTTPException(status_code=400, detail="until must be after since")
    return EpisodeFilters(start, end, tier, resource, tenant_id, agent_id, kind, open_only)


def summary_filters(
    since: datetime | None = Query(default=None, description="Episodes that started at or after this instant."),
    until: datetime | None = Query(default=None, description="Episodes that started before this instant."),
    tier: OrganizationPlanId | None = Query(default=None, description="Tier recorded when the episode opened."),
    resource: PressureResource | None = None,
    tenant_id: UUID | None = None,
) -> EpisodeFilters:
    return _filters(since, until, tier, resource, tenant_id)


def episode_filters(
    since: datetime | None = Query(default=None, description="Episodes that started at or after this instant."),
    until: datetime | None = Query(default=None, description="Episodes that started before this instant."),
    tier: OrganizationPlanId | None = Query(default=None, description="Tier recorded when the episode opened."),
    resource: PressureResource | None = None,
    tenant_id: UUID | None = None,
    agent_id: UUID | None = None,
    kind: PressureKind | None = None,
    open_only: bool = False,
) -> EpisodeFilters:
    return _filters(since, until, tier, resource, tenant_id, agent_id, kind, open_only)


@router.get("/summary", response_model=list[ResourcePressureTenantSummary])
async def get_resource_pressure_summary(
    filters: EpisodeFilters = Depends(summary_filters),
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> list[ResourcePressureTenantSummary]:
    del current_user
    return await tenant_summaries(filters)


@router.get("/episodes", response_model=ResourcePressureEpisodePage)
async def list_resource_pressure_episodes(
    filters: EpisodeFilters = Depends(episode_filters),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: str | None = Query(default=None),
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> ResourcePressureEpisodePage:
    del current_user
    return await episode_page(filters, cursor=cursor, limit=limit)


@export_router.get(
    "/export.csv",
    response_class=StreamingResponse,
    response_model=None,
    responses={
        200: {
            "description": "CSV of the matching episodes, newest first.",
            "content": {"text/csv": {"schema": {"type": "string"}}},
        }
    },
)
async def export_resource_pressure_episodes(
    filters: EpisodeFilters = Depends(episode_filters),
    current_user: UserRecord = Depends(require_role("platform_admin")),
) -> StreamingResponse:
    del current_user
    return StreamingResponse(
        stream_episodes_csv(filters),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="resource-pressure-episodes.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
