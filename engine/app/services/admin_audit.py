"""Admin action trail: actor, action, time, and field-level changes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from uuid import UUID

from app.core.json_types import JsonObject
from app.core.logging import logger
from app.dao.admin_audit_dao import admin_audit_log_dao
from app.db.session import optional_connection_ctx
from app.records.user import UserRecord


def field_change(before: object, after: object) -> dict[str, object]:
    return {"before": before, "after": after}


@asynccontextmanager
async def _audit_savepoint() -> AsyncIterator[None]:
    """Recover SQL failures before the best-effort writer swallows them."""
    async with optional_connection_ctx() as db:
        if db is None:
            yield
            return
        # SQL starts the outer transaction even on a freshly checked-out connection.
        # raw.transaction() on an idle connection would commit the audit independently.
        await db.execute("SAVEPOINT admin_audit_write")
        try:
            yield
        except Exception:
            await db.execute("ROLLBACK TO SAVEPOINT admin_audit_write")
            raise
        finally:
            await db.execute("RELEASE SAVEPOINT admin_audit_write")


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
        async with _audit_savepoint():
            _ = await admin_audit_log_dao.create(
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
