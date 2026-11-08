"""Authenticated opaque directory cursors scoped to the member and company."""

from base64 import urlsafe_b64encode
from hashlib import sha256
from typing import ClassVar, Literal
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.config import get_settings
from app.schemas.agent_directory import AgentDirectoryPosition


def _cipher() -> Fernet:
    key = sha256(("agent-directory-v1:" + get_settings().SECRET_KEY).encode()).digest()
    return Fernet(urlsafe_b64encode(key))


class AgentDirectoryCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    user_id: UUID
    tenant_id: UUID | None
    created_at: AwareDatetime | None
    id: UUID

    def encode(self) -> str:
        return _cipher().encrypt(self.model_dump_json().encode()).decode()


def parse_agent_directory_cursor(
    raw: str | None, user_id: UUID, tenant_id: UUID | None
) -> AgentDirectoryPosition | None:
    if raw is None:
        return None
    try:
        cursor = AgentDirectoryCursor.model_validate_json(_cipher().decrypt(raw.encode()))
    except InvalidToken, ValueError:
        raise HTTPException(400, "Invalid agent directory cursor") from None
    if cursor.user_id != user_id or cursor.tenant_id != tenant_id:
        raise HTTPException(400, "Agent directory cursor belongs to another member or company")
    return AgentDirectoryPosition(created_at=cursor.created_at, id=cursor.id)
