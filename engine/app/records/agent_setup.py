"""Durable, resolved inputs and ownership for ordinary agent creation."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.core.json_types import datetime_from_row, uuid_from_row, uuid_from_row_opt

SetupState = Literal["pending", "running", "succeeded", "failed"]
_state = TypeAdapter(SetupState)


@final
class AgentSetupInputs(BaseModel):
    model_config = ConfigDict(frozen=True)

    files: dict[str, str] = Field(default_factory=dict[str, str])
    gateway_key_encrypted: str
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentSetupRecord:
    agent_id: UUID
    inputs: AgentSetupInputs
    state: SetupState = "pending"
    claim_token: UUID | None = None
    lease_until: datetime | None = None
    failure_code: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentSetupRecord:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            inputs=AgentSetupInputs.model_validate(row["inputs"]),
            state=_state.validate_python(row["state"]),
            claim_token=uuid_from_row_opt(row.get("claim_token")),
            lease_until=datetime_from_row(row.get("lease_until")),
            failure_code=str(row["failure_code"]) if row.get("failure_code") is not None else None,
        )
