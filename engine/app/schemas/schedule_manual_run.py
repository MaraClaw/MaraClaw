from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.records.schedule_manual_run import ScheduleManualRunRecord

type ManualRunStatus = Literal["pending", "running", "success", "failure", "cancelled", "unknown"]


class ScheduleManualRunResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    run_id: UUID
    schedule_id: UUID
    agent_id: UUID
    requester_id: UUID
    status: ManualRunStatus
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    can_cancel: bool

    @classmethod
    def from_record(cls, run: ScheduleManualRunRecord) -> ScheduleManualRunResponse:
        status: ManualRunStatus
        if run.finished_at is None:
            status = "pending" if run.started_at is None else "running"
        else:
            match run.outcome:
                case "success":
                    status = "success"
                case "failure":
                    status = "failure"
                case "cancelled":
                    status = "cancelled"
                case _:
                    status = "unknown"
        return cls(
            run_id=run.id,
            schedule_id=run.schedule_id,
            agent_id=run.agent_id,
            requester_id=run.requester_id,
            status=status,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            can_cancel=status == "pending",
        )
