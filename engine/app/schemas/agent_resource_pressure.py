"""Platform-admin responses for agent resource-pressure episodes (tier-upsell evidence)."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.json_types import JsonObject
from app.records.agent_resource_pressure_episode import PressureKind, PressureResource
from app.records.organization_plan import OrganizationPlanId


class ResourcePressureEpisodeCounts(BaseModel):
    """Threshold episodes per resource; OOM kills are counted separately."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    memory: int
    cpu: int
    workspace_disk: int
    managed_storage: int


class ResourcePressureTenantSummary(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    tenant_id: UUID
    tenant_name: str
    current_tier: OrganizationPlanId | None
    episode_counts: ResourcePressureEpisodeCounts
    oom_count: int
    pressure_seconds: int = Field(description="Sum of each threshold episode's first-to-last sample span, in seconds.")
    max_peak_ratio: float
    agents_affected: int
    last_seen_at: datetime
    suggested_tier: OrganizationPlanId | None


class ResourcePressureEpisodeOut(BaseModel):
    """Units: bytes for memory, workspace_disk and managed_storage; millicpus for cpu."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    tenant_name: str
    agent_id: UUID | None
    agent_name: str
    tier: OrganizationPlanId
    resource: PressureResource
    kind: PressureKind
    limit_value: int
    peak_value: int
    peak_ratio: float
    threshold_ratio: float
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None
    sample_count: int
    detail: JsonObject


class ResourcePressureEpisodePage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: tuple[ResourcePressureEpisodeOut, ...]
    next_cursor: str | None
