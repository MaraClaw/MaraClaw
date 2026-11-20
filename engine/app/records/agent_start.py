from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.core.json_types import uuid_from_row

# Why a running start must stop at its next checkpoint: its operation is no longer pending (Stop cancelled it or
# a later start replaced it), or it is pending but the agent no longer meets the start's active condition.
type StartHalt = Literal["cancelled", "ineligible"]


@dataclass(frozen=True, slots=True)
class AgentStartOperation:
    agent_id: UUID
    operation_id: UUID
    state: str

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentStartOperation:
        return cls(uuid_from_row(row["agent_id"]), uuid_from_row(row["operation_id"]), str(row["state"]))
