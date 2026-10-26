"""Retry history is separate from the immutable start operation identity."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import datetime_from_row, int_from_row, uuid_from_row

StartFailureCode = Literal[
    "capacity_exceeded",
    "configuration_failed",
    "readiness_failed",
    "ownership_conflict",
    "runtime_failed",
    "publication_failed",
    "upgrade_in_progress",
]
_failure_code = TypeAdapter(StartFailureCode | None)


@dataclass(frozen=True, slots=True)
class AgentStartRetryMetadata:
    operation_id: UUID
    attempts: int
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    last_failure_code: StartFailureCode | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentStartRetryMetadata:
        return cls(
            operation_id=uuid_from_row(row["operation_id"]),
            attempts=int_from_row(row["attempts"]),
            last_attempt_at=datetime_from_row(row["last_attempt_at"]),
            next_retry_at=datetime_from_row(row["next_retry_at"]),
            last_failure_code=_failure_code.validate_python(row["last_failure_code"]),
        )
