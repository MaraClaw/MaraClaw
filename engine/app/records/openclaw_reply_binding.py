"""Durable authorization epoch for one web-visible native session."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import uuid_from_row, uuid_from_row_opt

_cursor = TypeAdapter(dict[str, int])


@dataclass(frozen=True, slots=True)
class OpenClawReplyBinding:
    id: UUID
    agent_id: UUID
    session_id: UUID
    owner_id: UUID
    tenant_id: UUID | None
    user_id: UUID
    cursor: dict[str, int]

    @property
    def session_key(self) -> str:
        return f"agent:main:hook:maraclaw:{self.id}"

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> OpenClawReplyBinding:
        return cls(
            uuid_from_row(row["id"]),
            uuid_from_row(row["agent_id"]),
            uuid_from_row(row["session_id"]),
            uuid_from_row(row["owner_id"]),
            uuid_from_row_opt(row["tenant_id"]),
            uuid_from_row(row["user_id"]),
            _cursor.validate_python(row["cursor"]),
        )
