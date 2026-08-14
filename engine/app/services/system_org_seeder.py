"""Ensure the MaraClaw and OpenClaw system organizations exist."""

from __future__ import annotations

from app.core.logging import logger
from app.dao.tenant_dao import tenant_dao
from app.records.tenant import TenantRecord

MARACLAW_SLUG = "maraclaw"
OPENCLAW_SLUG = "openclaw"


class SystemOrgSeedError(RuntimeError):
    """Raised when the default end-user organization cannot be resolved."""


async def ensure_system_orgs() -> tuple[TenantRecord, TenantRecord]:
    """Idempotently create the two system organizations.

    Returns ``(maraclaw, openclaw)``. Fails closed if OpenClaw cannot be resolved.
    Does not rename or reuse a legacy ``default`` slug.
    """
    openclaw = await _ensure_openclaw()
    maraclaw = await _ensure_maraclaw()
    return maraclaw, openclaw


async def _ensure_openclaw() -> TenantRecord:
    existing = await tenant_dao.get_by_slug(OPENCLAW_SLUG)
    if existing is None:
        existing = await tenant_dao.create(
            obj_in={
                "name": "OpenClaw",
                "slug": OPENCLAW_SLUG,
                "im_provider": "web_only",
                "is_system": True,
                "is_active": True,
            }
        )
        logger.info("[startup] Created OpenClaw organization")

    if existing is None:
        raise SystemOrgSeedError("OpenClaw organization could not be created")

    flagged = await tenant_dao.get_default_end_user_org()
    if flagged is None:
        existing = (
            await tenant_dao.update(db_obj=existing, obj_in={"is_default_end_user_org": True, "is_system": True})
            or existing
        )
    elif flagged.id != existing.id:
        logger.warning(
            "[startup] Default end-user org is already %s (%s); leaving OpenClaw unflagged",
            flagged.slug,
            flagged.id,
        )
        if not existing.is_system:
            existing = await tenant_dao.update(db_obj=existing, obj_in={"is_system": True}) or existing
    elif not existing.is_system:
        existing = await tenant_dao.update(db_obj=existing, obj_in={"is_system": True}) or existing

    resolved = await tenant_dao.get_default_end_user_org() or existing
    if resolved is None or not resolved.is_active:
        raise SystemOrgSeedError("No active default end-user organization")
    return resolved if flagged is None or flagged.id == existing.id else existing


async def _ensure_maraclaw() -> TenantRecord:
    existing = await tenant_dao.get_by_slug(MARACLAW_SLUG)
    if existing is None:
        existing = await tenant_dao.create(
            obj_in={
                "name": "MaraClaw",
                "slug": MARACLAW_SLUG,
                "im_provider": "web_only",
                "is_system": True,
                "is_active": True,
                "is_default_end_user_org": False,
            }
        )
        logger.info("[startup] Created MaraClaw organization")
        return existing
    if not existing.is_system:
        existing = await tenant_dao.update(db_obj=existing, obj_in={"is_system": True}) or existing
    return existing
