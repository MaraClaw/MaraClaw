"""Ensure the MaraClaw and KABERs system organizations exist."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.config import get_settings
from app.core.acl_locks import lock_tenants
from app.core.logging import logger
from app.dao.organization_admission_dao import organization_admission_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.services.organization_plans import plan_for

MARACLAW_SLUG = "maraclaw"
KABERS_SLUG = "kabers"
KABERS_NAME = "KABERs"

# The tier each system organization is governed by; bootstrap reconciles its approved admission to this at every start.
SYSTEM_ORG_TIERS: dict[str, OrganizationPlanId] = {
    MARACLAW_SLUG: OrganizationPlanId.ULTRA,
    KABERS_SLUG: OrganizationPlanId.PRO,
}

_EMAIL = TypeAdapter(EmailStr)
_PASSWORD_MIN_LENGTH = 6
_PASSWORD_MAX_LENGTH = 128


class SystemOrgSeedError(RuntimeError):
    """Raised when a system organization or the default organization admin cannot be ensured."""


async def ensure_system_orgs() -> tuple[TenantRecord, TenantRecord]:
    """Idempotently create the two system organizations and reconcile their tiers in one transaction.

    Returns ``(maraclaw, kabers)``. Any failure, including a missing or invalid default organization admin,
    rolls back every write.
    """
    async with connection_ctx() as db:
        kabers = await _ensure_kabers()
        maraclaw = await _ensure_maraclaw()
        for tenant in (maraclaw, kabers):
            await _ensure_tier(db, tenant)
        await _ensure_default_org_admin(kabers)
    return maraclaw, kabers


def _require_system_org(tenant: TenantRecord) -> None:
    if not (tenant.is_system or tenant.is_default_end_user_org):
        raise SystemOrgSeedError(f"Organization slug {tenant.slug!r} is used by an unrelated organization")
    if not tenant.is_active:
        raise SystemOrgSeedError(f"Organization {tenant.slug!r} is disabled")


async def _ensure_kabers() -> TenantRecord:
    existing = await tenant_dao.get_by_slug(KABERS_SLUG)
    if existing is not None:
        _require_system_org(existing)
    else:
        existing = await tenant_dao.create(
            obj_in={
                "name": KABERS_NAME,
                "slug": KABERS_SLUG,
                "im_provider": "web_only",
                "is_system": True,
                "is_active": True,
            }
        )
        logger.info("[startup] Created KABERs organization")

    flagged = await tenant_dao.get_default_end_user_org()
    if flagged is None:
        existing = (
            await tenant_dao.update(db_obj=existing, obj_in={"is_default_end_user_org": True, "is_system": True})
            or existing
        )
    elif flagged.id != existing.id:
        logger.warning(
            "[startup] Default end-user org is already %s (%s); leaving KABERs unflagged",
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


async def _ensure_tier(db: DbConnection, tenant: TenantRecord) -> None:
    """Make the approved admission linked to a system organization carry its tier and the current catalog plan.

    Tiers resolve only from approved admissions linked to the tenant, so this row is the tier. A missing row is
    inserted with a reserved non-deliverable address, an outdated tier or plan snapshot is refreshed in place,
    and an up-to-date row is left untouched. The reviewer and provisioned admin stay unset.
    """
    tier = SYSTEM_ORG_TIERS[tenant.slug]
    plan = plan_for(tier)
    # Concurrent bootstraps queue on the tenant row, so the later one sees the first one's row instead of adding another.
    await lock_tenants(db, (tenant.id,))
    linked = await organization_admission_dao.approved_plans_for_tenant(tenant.id)
    if len(linked) > 1:
        raise SystemOrgSeedError(f"Organization {tenant.slug!r} is linked to several approved admissions")

    snapshot = plan.model_dump(mode="json")
    if not linked:
        now = datetime.now(UTC)
        await organization_admission_dao.create(
            obj_in={
                "email": f"{tenant.slug}@system.invalid",
                "tier": tier.value,
                "plan": snapshot,
                "status": "approved",
                "organization_name": tenant.name,
                "reviewed_at": now,
                "review_note": "Approved automatically by system organization bootstrap.",
                "provisioned_at": now,
                "provisioned_tenant_id": tenant.id,
            }
        )
        logger.info("[startup] Approved %s organization as %s", tenant.slug, tier.value)
    elif linked[0].tier is not tier or linked[0].plan != snapshot:
        await organization_admission_dao.refresh_tier_and_plan(linked[0].id, tier=tier, plan=plan)
        logger.info("[startup] Refreshed %s organization admission to %s", tenant.slug, tier.value)


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


def _default_admin_credentials() -> tuple[str, str]:
    settings = get_settings()
    email = (settings.KABERS_ORG_ADMIN_EMAIL or "").strip().lower()
    password = settings.KABERS_ORG_ADMIN_PASSWORD or ""
    if not email or not password:
        raise SystemOrgSeedError(
            "Default organization genesis admin was not found. "
            "Set KABERS_ORG_ADMIN_EMAIL and KABERS_ORG_ADMIN_PASSWORD to seed it."
        )
    try:
        email = str(_EMAIL.validate_python(email))
    except ValidationError as exc:
        raise SystemOrgSeedError("KABERS_ORG_ADMIN_EMAIL must be a valid email address.") from exc
    if not _PASSWORD_MIN_LENGTH <= len(password) <= _PASSWORD_MAX_LENGTH or not password.strip():
        raise SystemOrgSeedError(
            f"KABERS_ORG_ADMIN_PASSWORD must be {_PASSWORD_MIN_LENGTH} to {_PASSWORD_MAX_LENGTH} "
            "characters and not blank."
        )
    return email, password


async def _ensure_default_org_admin(tenant: TenantRecord) -> None:
    """Seed the genesis org admin only when the tenant has none; an established one is never touched."""
    if await user_dao.genesis_org_admin_for_tenant(tenant.id) is not None:
        logger.info("[startup] Default organization genesis admin exists (tenant=%s); skipping env seed", tenant.id)
        return

    from app.services.tenant_provisioning import (
        AdminEmailTakenError,
        GenesisOrgAdminExistsError,
        attach_genesis_org_admin,
    )

    email, password = _default_admin_credentials()
    try:
        provisioned = await attach_genesis_org_admin(tenant_id=tenant.id, admin_email=email, admin_password=password)
    except AdminEmailTakenError as exc:
        raise SystemOrgSeedError(
            f"KABERS_ORG_ADMIN_EMAIL {email} already belongs to another account; use an unused email."
        ) from exc
    except GenesisOrgAdminExistsError:
        return
    logger.info(
        "[startup] Seeded default organization genesis admin %s (id=%s); password change required on first login",
        email,
        provisioned.org_admin.id,
    )
