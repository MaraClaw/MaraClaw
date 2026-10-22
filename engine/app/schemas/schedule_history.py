"""Schedule history payloads and versioned, outcome-bound positions."""

from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from app.core.json_types import json_as_str_or, object_mapping_from
from app.records.activity_log import AgentActivityLogRecord

type HistoryOutcome = Literal["all", "success", "failure", "cancelled", "unknown"]


class ScheduleHistoryItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: UUID
    created_at: str | None
    summary: str
    instruction: str
    reply: str
    status: Literal["success", "failure", "cancelled", "unknown"]

    @classmethod
    def from_record(cls, log: AgentActivityLogRecord) -> ScheduleHistoryItem:
        detail = object_mapping_from(log.detail_json)
        match detail.get("status"):
            case "success":
                run_status = "success"
            case "failure" | "failed":
                run_status = "failure"
            case "cancelled":
                run_status = "cancelled"
            case _:
                run_status = "unknown"
        return cls(
            id=log.id,
            created_at=log.created_at.isoformat() if log.created_at is not None else None,
            summary=log.summary,
            instruction=json_as_str_or(detail.get("instruction")),
            reply=json_as_str_or(detail.get("reply")),
            status=run_status,
        )


class ScheduleHistoryPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    items: list[ScheduleHistoryItem]
    next_cursor: str | None


class _HistoryPosition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    agent_id: UUID
    schedule_id: UUID
    created_at: AwareDatetime | None
    id: UUID


class LegacyHistoryCursor(_HistoryPosition):
    version: Literal[1]


class HistoryCursor(_HistoryPosition):
    version: Literal[2]
    outcome: HistoryOutcome


history_cursor_adapter = TypeAdapter(Annotated[LegacyHistoryCursor | HistoryCursor, Field(discriminator="version")])
