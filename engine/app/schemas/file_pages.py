"""Workspace file listing models: the shared row and the cursor-paged listing."""

import hashlib
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import ClassVar, Literal, Self
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

MAX_FILE_CURSOR_LENGTH = 4096


class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int = 0
    modified_at: str = ""
    version_token: str | None = None
    url: str | None = None


class FileInfoPage(BaseModel):
    """Rows in ``walk_order`` (byte order of full keys, folders interleaved with files), not folders first."""

    items: list[FileInfo]
    next_cursor: str | None = None


class FileListingCursor(BaseModel):
    """Opaque position bound to one agent and one resolved storage directory."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    agent_id: UUID
    scope: str
    after: str

    @staticmethod
    def scope_of(storage_key: str) -> str:
        return hashlib.sha256(storage_key.encode()).hexdigest()[:32]

    def encode(self) -> str:
        return urlsafe_b64encode(self.model_dump_json().encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Self:
        """Parse a cursor; anything malformed, non-canonical or oversized is a 400."""
        if not value or len(value) > MAX_FILE_CURSOR_LENGTH:
            raise HTTPException(400, "Invalid cursor")
        try:
            payload = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            position = cls.model_validate_json(payload, strict=True)
        except (Base64Error, ValueError) as exc:
            raise HTTPException(400, "Invalid cursor") from exc
        if urlsafe_b64encode(payload).decode().rstrip("=") != value:
            raise HTTPException(400, "Invalid cursor")
        return position

    def require_scope(self, agent_id: UUID, storage_key: str) -> None:
        if self.agent_id != agent_id or self.scope != self.scope_of(storage_key):
            raise HTTPException(400, "Invalid cursor")
