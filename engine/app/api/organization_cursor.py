from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from hashlib import sha256
from typing import ClassVar, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.dao.user_dao import DirectoryPagePosition


class DirectoryCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    tenant_id: UUID
    search_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    display_name: str = Field(max_length=100, pattern=r"^[^\x00]*$")
    user_id: UUID


def decode_cursor(cursor: str, tenant_id: UUID | None, search: str) -> DirectoryPagePosition:
    try:
        payload = b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        position = DirectoryCursor.model_validate_json(payload)
    except (Base64Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    if position.tenant_id != tenant_id or position.search_digest != sha256(search.encode()).hexdigest():
        raise HTTPException(status_code=400, detail="Cursor does not match tenant or search")
    return DirectoryPagePosition(position.display_name, position.user_id)


def encode_cursor(tenant_id: UUID, search: str, position: DirectoryPagePosition) -> str:
    payload = DirectoryCursor(
        version=1,
        tenant_id=tenant_id,
        search_digest=sha256(search.encode()).hexdigest(),
        display_name=position.display_name,
        user_id=position.user_id,
    )
    return urlsafe_b64encode(payload.model_dump_json().encode()).decode().rstrip("=")
