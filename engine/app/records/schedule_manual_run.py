from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


@dataclass(frozen=True, slots=True)
class ScheduleManualRunRecord:
    id: UUID
    schedule_id: UUID
    agent_id: UUID
    requester_id: UUID
    instruction: str
    idempotency_key: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str | None = None
    lease_token: UUID | None = None
    lease_expires_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ScheduleManualRunRecord:
        return cls(
            id=uuid_from_row(row["id"]),
            schedule_id=uuid_from_row(row["schedule_id"]),
            agent_id=uuid_from_row(row["agent_id"]),
            requester_id=uuid_from_row(row["requester_id"]),
            instruction=str_from_row(row["instruction"]),
            idempotency_key=str_from_row(row["idempotency_key"]) if row.get("idempotency_key") is not None else None,
            created_at=datetime_from_row(row.get("created_at")),
            started_at=datetime_from_row(row.get("started_at")),
            finished_at=datetime_from_row(row.get("finished_at")),
            outcome=str_from_row(row["outcome"]) if row.get("outcome") is not None else None,
            lease_token=uuid_from_row(row["lease_token"]) if row.get("lease_token") is not None else None,
            lease_expires_at=datetime_from_row(row.get("lease_expires_at")),
        )
