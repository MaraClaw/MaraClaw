"""Additive admin-audit page responses; legacy array models remain unchanged."""

from typing import ClassVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, JsonValue


class AdminAuditPageItem(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True, frozen=True)

    id: UUID
    actor_id: UUID | None = None
    actor_role: str
    actor_email: str | None = None
    action: str
    target_type: str
    target_id: UUID | None = None
    tenant_id: UUID | None = None
    changes: dict[str, JsonValue]
    details: dict[str, JsonValue]
    ip_address: str | None = None
    created_at: AwareDatetime


class AdminAuditPageOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[AdminAuditPageItem]
    next_cursor: str | None
