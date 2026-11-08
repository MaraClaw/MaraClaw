"""Private encrypted native MCP definitions; never serialized as API records."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from app.core.json_types import str_from_row, uuid_from_row


@dataclass(frozen=True, slots=True)
class AgentMCPServerRecord:
    agent_id: UUID
    name: str
    definition_ciphertext: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> AgentMCPServerRecord:
        return cls(
            agent_id=uuid_from_row(row["agent_id"]),
            name=str_from_row(row["name"]),
            definition_ciphertext=str_from_row(row["definition_ciphertext"]) if row["definition_ciphertext"] else None,
        )
