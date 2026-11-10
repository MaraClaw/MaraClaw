"""Authenticated objective cursors scoped to the tenant, effective period and filters."""

from base64 import b64decode, urlsafe_b64encode
from hashlib import sha256
from typing import ClassVar, Literal
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.config import get_settings
from app.dao.okr_dao import ObjectivePagePosition, ObjectivePageScope
from app.records.okr import OKRObjectiveRecord


def _cipher() -> Fernet:
    key = sha256(("okr-objectives-v1:" + get_settings().SECRET_KEY).encode()).digest()
    return Fernet(urlsafe_b64encode(key))


class ObjectivePageCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    scope: ObjectivePageScope
    owner_type: str
    created_at: AwareDatetime
    id: UUID


def encode_objective_cursor(scope: ObjectivePageScope, last: OKRObjectiveRecord) -> str:
    """Encode the last retained persisted row, never the lookahead."""
    if last.created_at is None:
        raise RuntimeError("Persisted OKR objective has no creation timestamp")
    cursor = ObjectivePageCursor(scope=scope, owner_type=last.owner_type, created_at=last.created_at, id=last.id)
    return _cipher().encrypt(cursor.model_dump_json().encode()).decode()


def parse_objective_cursor(raw: str | None, scope: ObjectivePageScope) -> ObjectivePagePosition | None:
    """Reject malformed or foreign-scope cursors before reading objectives."""
    if raw is None:
        return None
    try:
        _ = b64decode(raw, altchars=b"-_", validate=True)
        cursor = ObjectivePageCursor.model_validate_json(_cipher().decrypt(raw.encode()))
    except InvalidToken, ValueError:
        raise HTTPException(400, "Invalid objective cursor") from None
    if cursor.scope != scope:
        raise HTTPException(400, "Objective cursor belongs to another tenant, period or filter")
    return ObjectivePagePosition(cursor.owner_type, cursor.created_at, cursor.id)
