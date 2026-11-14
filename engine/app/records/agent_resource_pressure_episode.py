"""Agent resource-pressure episode row: a continuous stretch at or above the threshold of a limit, or one OOM kill."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.core.json_types import (
    datetime_from_row,
    float_from_row,
    int_from_row,
    mapping_from_row,
    str_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.records.organization_plan import OrganizationPlanId


class PressureResource(StrEnum):
    """Measured resource; units are bytes except ``cpu`` (millicpus)."""

    MEMORY = "memory"
    CPU = "cpu"
    WORKSPACE_DISK = "workspace_disk"
    MANAGED_STORAGE = "managed_storage"


class PressureKind(StrEnum):
    THRESHOLD = "threshold"
    OOM_KILL = "oom_kill"


@dataclass(slots=True)
class AgentResourcePressureEpisodeRecord:
    id: UUID
    tenant_id: UUID
    agent_name: str
    tier: OrganizationPlanId
    resource: PressureResource
    limit_value: int
    peak_value: int
    peak_ratio: float
    threshold_ratio: float
    started_at: datetime
    last_seen_at: datetime
    agent_id: UUID | None = None
    kind: PressureKind = PressureKind.THRESHOLD
    ended_at: datetime | None = None
    sample_count: int = 1
    detail: dict[str, Any] = field(default_factory=dict[str, Any])
    event_key: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentResourcePressureEpisodeRecord:
        started_at = datetime_from_row(row["started_at"])
        last_seen_at = datetime_from_row(row["last_seen_at"])
        if started_at is None or last_seen_at is None:
            raise ValueError("agent_resource_pressure_episodes timestamps are NOT NULL")
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            agent_id=uuid_from_row_opt(row.get("agent_id")),
            agent_name=str_from_row(row["agent_name"]),
            tier=OrganizationPlanId(str_from_row(row["tier"])),
            resource=PressureResource(str_from_row(row["resource"])),
            kind=PressureKind(str_from_row(row["kind"])),
            limit_value=int_from_row(row["limit_value"]),
            peak_value=int_from_row(row["peak_value"]),
            peak_ratio=float_from_row(row["peak_ratio"]),
            threshold_ratio=float_from_row(row["threshold_ratio"]),
            started_at=started_at,
            last_seen_at=last_seen_at,
            ended_at=datetime_from_row(row.get("ended_at")),
            sample_count=int_from_row(row["sample_count"], 1),
            detail=mapping_from_row(row.get("detail")),
            event_key=None if row.get("event_key") is None else str_from_row(row["event_key"]),
        )
