"""Admin action trail: actor, action, time, and field-level changes."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from psycopg.pq import TransactionStatus

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.db.session import get_connection
from app.records.user import UserRecord


def field_change(before: object, after: object) -> dict[str, object]:
    return {"before": before, "after": after}


async def write_admin_audit(
    *,
    actor: UserRecord,
    action: str,
    target_type: str,
    target_id: UUID | None = None,
    tenant_id: UUID | None = None,
    changes: Mapping[str, object] | JsonObject | None = None,
    details: Mapping[str, object] | JsonObject | None = None,
    ip_address: str | None = None,
) -> None:
    """Persist one admin action. Failures are logged and never raised."""
    try:
        await _insert_admin_audit(
            {
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


async def _insert_admin_audit(obj_in: dict[str, object]) -> None:
    """Insert one audit row, rolling a failed insert back to a local savepoint.

    PostgreSQL aborts the whole transaction after a failed statement. The
    best-effort logger swallows that error, so the insert has to run inside a
    savepoint or the request transaction cannot continue. ``transaction()``
    commits when it begins the outer transaction; a preceding statement keeps
    it nested so the request connection still owns the commit.
    """
    bound = get_connection()
    if bound is None:
        _ = await admin_audit_log_dao.create(obj_in=obj_in)
        return
    if bound.raw.info.transaction_status == TransactionStatus.IDLE:
        await bound.execute("SELECT 1")
    async with bound.raw.transaction():
        _ = await admin_audit_log_dao.create(obj_in=obj_in)
