"""Admin action trail: actor, action, time, and field-level changes."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.records.user import UserRecord


def field_change(before: Any, after: Any) -> dict[str, Any]:
    return {"before": before, "after": after}


async def write_admin_audit(
    *,
    actor: UserRecord,
    action: str,
    target_type: str,
    target_id: UUID | None = None,
    tenant_id: UUID | None = None,
    changes: JsonObject | None = None,
    details: JsonObject | None = None,
    ip_address: str | None = None,
) -> None:
    """Persist one admin action. Failures are logged and never raised."""
    try:
        await admin_audit_log_dao.create(
            obj_in={
                "actor_id": actor.id,
                "actor_role": getattr(actor, "role", "") or "",
                "actor_email": getattr(actor, "email", None),
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "tenant_id": tenant_id,
                "changes": dict(changes or {}),
                "details": dict(details or {}),
                "ip_address": ip_address,
            }
        )
    except Exception as exc:
        logger.error("[admin_audit] failed to write {}: {}", action, exc)
