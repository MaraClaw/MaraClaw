"""Platform-admin reading of resource-pressure episodes: per-organization summary, tier suggestion, pages and CSV.

Authorization belongs to the caller and must precede cursor decoding.
"""

import csv
import io
import json
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Final
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.config import get_settings
from app.core.json_types import json_object_from
from app.dao.agent_resource_pressure_episode_dao import (
    EpisodeFilters,
    EpisodeListing,
    EpisodePosition,
    agent_resource_pressure_episode_dao,
)
from app.dao.organization_admission_dao import organization_admission_dao
from app.records.agent_resource_pressure_episode import PressureResource
from app.records.organization_plan import OrganizationPlan, OrganizationPlanId
from app.schemas.agent_resource_pressure import (
    ResourcePressureEpisodeCounts,
    ResourcePressureEpisodeOut,
    ResourcePressureEpisodePage,
    ResourcePressureTenantSummary,
)
from app.services.organization_plans import ORGANIZATION_PLANS, plan_for

DEFAULT_PAGE_SIZE: Final = 50
MAX_PAGE_SIZE: Final = 200
MAX_CURSOR_LENGTH: Final = 512
EXPORT_PAGE_SIZE: Final = 500
TIERS: Final = tuple(plan.id for plan in ORGANIZATION_PLANS)
CSV_COLUMNS: Final = (
    "id",
    "tenant_id",
    "tenant_name",
    "agent_id",
    "agent_name",
    "tier",
    "resource",
    "kind",
    "limit_value",
    "peak_value",
    "peak_ratio",
    "threshold_ratio",
    "started_at",
    "last_seen_at",
    "ended_at",
    "sample_count",
    "detail",
)


@dataclass(frozen=True, slots=True)
class ResourceDemand:
    """Highest value seen for one resource at the organization's current tier, and whether a guest was OOM-killed."""

    resource: PressureResource
    peak_value: int
    oom: bool = False


def resource_limit(plan: OrganizationPlan, resource: PressureResource) -> int:
    """The plan's limit for ``resource``, in the unit episodes use (bytes; millicpus for CPU)."""
    match resource:
        case PressureResource.MEMORY:
            return int(plan.guest_memory_bytes)
        case PressureResource.CPU:
            return int(plan.guest_millicpus)
        case PressureResource.WORKSPACE_DISK:
            return int(plan.workspace_disk_bytes)
        case PressureResource.MANAGED_STORAGE:
            return int(plan.storage_limit_bytes)


def suggest_resource_tier(
    current: OrganizationPlanId, demand: ResourceDemand, threshold: float
) -> OrganizationPlanId | None:
    """Smallest tier above ``current`` whose limit keeps the observed peak under ``threshold`` of it.

    Only tiers above the current one are candidates, so an OOM kill (which also counts the kill as a peak at the
    limit) at the current tier's limit yields at least the next tier. ``None`` when ``current`` is the top tier, no
    tier fits, or the peak already fits the current tier: a guest still running with an older, lower tier's limits
    gets the current limits on its next resume, so its pressure (or kill) there is no reason to upgrade again.
    """
    if demand.peak_value < threshold * resource_limit(plan_for(current), demand.resource):
        return None
    for tier in TIERS[TIERS.index(current) + 1 :]:
        if demand.peak_value < threshold * resource_limit(plan_for(tier), demand.resource):
            return tier
    return None


def suggest_tier(
    current: OrganizationPlanId, demands: Iterable[ResourceDemand], threshold: float
) -> OrganizationPlanId | None:
    """Highest of the per-resource suggestions: the organization needs every resource that can be relieved."""
    suggestions = [suggest_resource_tier(current, demand, threshold) for demand in demands]
    fitting = [tier for tier in suggestions if tier is not None]
    return max(fitting, key=TIERS.index, default=None)


async def tenant_summaries(filters: EpisodeFilters) -> list[ResourcePressureTenantSummary]:
    """One row per organization with episodes under ``filters``, most recently seen first.

    The suggestion reads only episodes recorded under the organization's current tier, so a tier change starts
    from a clean slate; an organization without exactly one approved tier gets none.
    """
    aggregates = await agent_resource_pressure_episode_dao.tenant_aggregates(filters)
    if not aggregates:
        return []
    peaks = await agent_resource_pressure_episode_dao.tenant_resource_peaks(filters)
    linked = await organization_admission_dao.approved_tiers_for_tenants([row.tenant_id for row in aggregates])
    threshold = get_settings().AGENT_RESOURCE_PRESSURE_THRESHOLD
    summaries: list[ResourcePressureTenantSummary] = []
    for row in aggregates:
        tiers = linked.get(row.tenant_id, [])
        current = tiers[0] if len(tiers) == 1 else None
        suggested = None
        if current is not None:
            demands = [
                ResourceDemand(peak.resource, peak.peak_value, peak.oom)
                for peak in peaks
                if peak.tenant_id == row.tenant_id and peak.tier is current
            ]
            suggested = suggest_tier(current, demands, threshold)
        summaries.append(
            ResourcePressureTenantSummary(
                tenant_id=row.tenant_id,
                tenant_name=row.tenant_name,
                current_tier=current,
                episode_counts=ResourcePressureEpisodeCounts(
                    memory=row.episode_counts[PressureResource.MEMORY],
                    cpu=row.episode_counts[PressureResource.CPU],
                    workspace_disk=row.episode_counts[PressureResource.WORKSPACE_DISK],
                    managed_storage=row.episode_counts[PressureResource.MANAGED_STORAGE],
                ),
                oom_count=row.oom_count,
                pressure_seconds=row.pressure_seconds,
                max_peak_ratio=row.max_peak_ratio,
                agents_affected=row.agents_affected,
                last_seen_at=row.last_seen_at,
                suggested_tier=suggested,
            )
        )
    return summaries


class EpisodeCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True, ge=1, le=1)
    started_at: AwareDatetime
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> EpisodeCursor:
        if not value or len(value) > MAX_CURSOR_LENGTH:
            raise HTTPException(status_code=400, detail="Invalid cursor")
        try:
            raw = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            return cls.model_validate_json(raw, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid cursor") from exc


def _episode_out(listing: EpisodeListing) -> ResourcePressureEpisodeOut:
    episode = listing.episode
    return ResourcePressureEpisodeOut(
        id=episode.id,
        tenant_id=episode.tenant_id,
        tenant_name=listing.tenant_name,
        agent_id=episode.agent_id,
        agent_name=episode.agent_name,
        tier=episode.tier,
        resource=episode.resource,
        kind=episode.kind,
        limit_value=episode.limit_value,
        peak_value=episode.peak_value,
        peak_ratio=episode.peak_ratio,
        threshold_ratio=episode.threshold_ratio,
        started_at=episode.started_at,
        last_seen_at=episode.last_seen_at,
        ended_at=episode.ended_at,
        sample_count=episode.sample_count,
        detail=json_object_from(episode.detail),
    )


async def episode_page(filters: EpisodeFilters, *, cursor: str | None, limit: int) -> ResourcePressureEpisodePage:
    """Keyset page, newest first. The cursor is only a position: the caller repeats the filters."""
    boundary = EpisodeCursor.decode(cursor) if cursor is not None else None
    after = None if boundary is None else EpisodePosition(boundary.started_at, boundary.id)
    listings = await agent_resource_pressure_episode_dao.list_page(filters, after=after, limit=limit + 1)
    page = listings[:limit]
    next_cursor = None
    if len(listings) > limit:
        last = page[-1].episode
        next_cursor = EpisodeCursor(version=1, started_at=last.started_at, id=last.id).encode()
    return ResourcePressureEpisodePage(items=tuple(_episode_out(listing) for listing in page), next_cursor=next_cursor)


def csv_text_cell(value: str) -> str:
    """Defuse spreadsheet formulas in user-controlled text (tenant and agent names)."""
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _timestamp(value: datetime | None) -> str:
    return "" if value is None else value.isoformat()


def episode_csv_row(listing: EpisodeListing) -> list[str | int | float]:
    episode = listing.episode
    return [
        str(episode.id),
        str(episode.tenant_id),
        csv_text_cell(listing.tenant_name),
        "" if episode.agent_id is None else str(episode.agent_id),
        csv_text_cell(episode.agent_name),
        episode.tier.value,
        episode.resource.value,
        episode.kind.value,
        episode.limit_value,
        episode.peak_value,
        episode.peak_ratio,
        episode.threshold_ratio,
        _timestamp(episode.started_at),
        _timestamp(episode.last_seen_at),
        _timestamp(episode.ended_at),
        episode.sample_count,
        json.dumps(episode.detail, sort_keys=True, separators=(",", ":")),
    ]


def _csv_chunk(rows: Iterable[Iterable[str | int | float]]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8")


async def stream_episodes_csv(filters: EpisodeFilters) -> AsyncIterator[bytes]:
    """CSV of every episode under ``filters``, newest first, read in keyset pages (one short query each)."""
    yield _csv_chunk([CSV_COLUMNS])
    after: EpisodePosition | None = None
    while True:
        page = await agent_resource_pressure_episode_dao.list_page(filters, after=after, limit=EXPORT_PAGE_SIZE)
        if page:
            yield _csv_chunk(episode_csv_row(listing) for listing in page)
        if len(page) < EXPORT_PAGE_SIZE:
            return
        last = page[-1].episode
        after = EpisodePosition(last.started_at, last.id)
