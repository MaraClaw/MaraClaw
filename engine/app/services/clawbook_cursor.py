"""Versioned opaque Clawbook positions, bound to the exact effective read scope."""

import base64
from typing import ClassVar, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.dao.clawbook_dao import ClawbookPosition


class ClawbookCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    tenant_id: UUID | None
    post_id: UUID | None
    created_at: AwareDatetime
    id: UUID

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")


def parse_clawbook_cursor(
    raw: str | None, tenant_id: UUID | None, post_id: UUID | None = None
) -> ClawbookPosition | None:
    if raw is None:
        return None
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        cursor = ClawbookCursor.model_validate_json(decoded)
    except ValueError:
        raise HTTPException(400, "Invalid Clawbook cursor") from None
    if cursor.tenant_id != tenant_id or cursor.post_id != post_id:
        raise HTTPException(400, "Clawbook cursor belongs to another tenant or scope")
    return ClawbookPosition(created_at=cursor.created_at, id=cursor.id)
