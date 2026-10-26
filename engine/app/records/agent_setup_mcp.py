"""Safe optional import outcomes and durable queue ownership."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import datetime_from_row, int_from_row, uuid_from_row, uuid_from_row_opt

MCPWarningCode = Literal[
    "missing_credentials",
    "authorization_required",
    "unsupported_remote_transport",
    "connection_failed",
    "tools_unverified",
    "import_failed",
]
MCPWorkState = Literal["pending", "running", "succeeded", "warning"]
_warning = TypeAdapter(MCPWarningCode | None)
_state = TypeAdapter(MCPWorkState)


@dataclass(frozen=True, slots=True)
class AgentSetupMCPWork:
    agent_id: UUID
    server_id: str
    owner_id: UUID
    tenant_id: UUID | None
    requester_id: UUID
    state: MCPWorkState
    warning_code: MCPWarningCode | None
    attempts: int
    claim_token: UUID | None
    lease_until: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentSetupMCPWork:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            server_id=str(row["server_id"]),
            owner_id=uuid_from_row(row["owner_id"]),
            tenant_id=uuid_from_row_opt(row["tenant_id"]),
            requester_id=uuid_from_row(row["requester_id"]),
            state=_state.validate_python(row["state"]),
            warning_code=_warning.validate_python(row["warning_code"]),
            attempts=int_from_row(row["attempts"]),
            claim_token=uuid_from_row_opt(row["claim_token"]),
            lease_until=datetime_from_row(row["lease_until"]),
        )
