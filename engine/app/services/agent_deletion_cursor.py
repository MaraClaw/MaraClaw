from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import ClassVar
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.records.agent_deletion_request import DeletionStatus


class DeletionCursor(BaseModel):
    """Keyset position bound to the exact filters that produced it."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(strict=True, ge=1, le=1)
    tenant_id: UUID
    requester_id: UUID | None
    status: DeletionStatus | None
    agent_id: UUID | None
    created_at: AwareDatetime
    id: UUID

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, cursor: str) -> DeletionCursor:
        try:
            payload = b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            return cls.model_validate_json(payload, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(400, "Invalid cursor") from exc
