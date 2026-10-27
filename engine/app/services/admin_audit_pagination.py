"""Role/scope-bound audit seek cursors, not cross-request snapshots.

New inserts and late commits may require refreshing from a new root page.
Authorization belongs to the caller and must precede cursor decoding.
"""

import re
from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import ClassVar, Final, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.dao.admin_audit_dao import admin_audit_log_dao
from app.schemas.admin_audit import AdminAuditPageItem, AdminAuditPageOut

MAX_CURSOR_LENGTH = 1024
_RFC3339_BOUND: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def parse_admin_audit_bound(value: str | None) -> datetime | None:
    if value is None:
        return None
    if not _RFC3339_BOUND.fullmatch(value):
        raise HTTPException(status_code=422, detail="Audit bounds must be RFC 3339 timestamps with an offset")
    try:
        return datetime.fromisoformat(value.upper()).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail="Invalid audit timestamp") from exc


@dataclass(frozen=True, slots=True)
class AdminAuditScope:
    role: Literal["platform_admin", "org_admin"]
    tenant_id: UUID | None = None
    actor_id: UUID | None = None
    action: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


class _CursorScope(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    role: Literal["platform_admin", "org_admin"]
    tenant_id: UUID | None
    actor_id: UUID | None
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_at: AwareDatetime | None
    end_at: AwareDatetime | None


class _AuditCursor(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(strict=True, ge=2, le=2)
    scope: _CursorScope
    created_at: AwareDatetime
    id: UUID


def _decode_cursor(value: str, scope: _CursorScope) -> _AuditCursor:
    if not value or len(value) > MAX_CURSOR_LENGTH:
        raise HTTPException(status_code=400, detail="Invalid cursor")
    try:
        raw = b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = _AuditCursor.model_validate_json(raw, strict=True)
    except (Base64Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    if cursor.scope != scope:
        raise HTTPException(status_code=400, detail="Invalid cursor")
    return cursor


async def list_admin_audit_page(
    scope: AdminAuditScope, *, cursor: str | None = None, limit: int = 100
) -> AdminAuditPageOut:
    """Read older events for an already-authorized effective role and scope."""
    page_size = min(max(limit, 1), 500)
    start_at = scope.start_at.astimezone(UTC) if scope.start_at is not None else None
    end_at = scope.end_at.astimezone(UTC) if scope.end_at is not None else None
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise HTTPException(status_code=422, detail="start_at must be earlier than end_at")
    cursor_scope = _CursorScope(
        role=scope.role,
        tenant_id=scope.tenant_id,
        actor_id=scope.actor_id,
        action_digest=sha256((scope.action or "").encode()).hexdigest(),
        start_at=start_at,
        end_at=end_at,
    )
    position = _decode_cursor(cursor, cursor_scope) if cursor is not None else None
    rows = await admin_audit_log_dao.list_page(
        tenant_id=scope.tenant_id,
        actor_id=scope.actor_id,
        action=scope.action,
        start_at=start_at,
        end_at=end_at,
        limit=page_size,
        before=(position.created_at, position.id) if position is not None else None,
    )
    items = [AdminAuditPageItem.model_validate(row) for row in rows[:page_size]]
    next_cursor = None
    if len(rows) > page_size:
        last = items[-1]
        boundary = _AuditCursor(version=2, scope=cursor_scope, created_at=last.created_at, id=last.id)
        next_cursor = urlsafe_b64encode(boundary.model_dump_json().encode()).decode().rstrip("=")
    return AdminAuditPageOut(items=items, next_cursor=next_cursor)
