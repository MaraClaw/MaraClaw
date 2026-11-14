"""Per-agent measured workspace disk usage row (host directory plus guest writable layer)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, uuid_from_row


@dataclass(slots=True)
class AgentWorkspaceUsageRecord:
    agent_id: UUID
    dir_bytes: int
    layer_bytes: int
    limit_bytes: int
    measured_at: datetime | None = None
    over_since: datetime | None = None
    notified_at: datetime | None = None

    @property
    def used_bytes(self) -> int:
        return self.dir_bytes + self.layer_bytes

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentWorkspaceUsageRecord:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            dir_bytes=int_from_row(row["dir_bytes"]),
            layer_bytes=int_from_row(row["layer_bytes"]),
            limit_bytes=int_from_row(row["limit_bytes"]),
            measured_at=datetime_from_row(row.get("measured_at")),
            over_since=datetime_from_row(row.get("over_since")),
            notified_at=datetime_from_row(row.get("notified_at")),
        )
