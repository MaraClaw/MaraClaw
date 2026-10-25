"""Read-only, secret-free projection of durable trigger executions."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import AwareDatetime, TypeAdapter

from app.core.domain.triggers import TriggerExecutionStatus


@dataclass(frozen=True, slots=True)
class TriggerExecutionHistoryRecord:
    id: UUID
    trigger_id: UUID
    agent_id: UUID
    source: str
    status: TriggerExecutionStatus
    scheduled_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    created_at: AwareDatetime

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> TriggerExecutionHistoryRecord:
        return _history_adapter.validate_python(row)


_history_adapter = TypeAdapter(TriggerExecutionHistoryRecord)
