from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.core.json_types import uuid_from_row


@dataclass(frozen=True, slots=True)
class AgentStartOperation:
    agent_id: UUID
    operation_id: UUID
    state: str

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentStartOperation:
        return cls(uuid_from_row(row["agent_id"]), uuid_from_row(row["operation_id"]), str(row["state"]))
