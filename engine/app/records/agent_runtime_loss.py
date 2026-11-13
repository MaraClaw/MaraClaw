"""Recorded loss of an agent's guest container, kept until a start publishes a new one."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import datetime_from_row, uuid_from_row

RuntimeLossReason = Literal["container_missing", "image_unavailable"]
_reason = TypeAdapter(RuntimeLossReason)


@dataclass(frozen=True, slots=True)
class AgentRuntimeLoss:
    agent_id: UUID
    reason: RuntimeLossReason
    recorded_container_id: str | None
    image: str | None
    detected_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentRuntimeLoss:
        detected_at = datetime_from_row(row["detected_at"])
        if detected_at is None:
            raise ValueError("agent_runtime_losses.detected_at is required")
        container = row["recorded_container_id"]
        image = row["image"]
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            reason=_reason.validate_python(row["reason"]),
            recorded_container_id=container if isinstance(container, str) else None,
            image=image if isinstance(image, str) else None,
            detected_at=detected_at,
        )
