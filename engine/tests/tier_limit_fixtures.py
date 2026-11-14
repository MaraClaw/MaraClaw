"""Set the organization tier of a tenant in a live PostgreSQL schema."""

from uuid import UUID

from app.db.connection import DbConnection
from app.records.organization_plan import OrganizationPlanId


async def set_tenant_tier(db: DbConnection, tenant_id: UUID, tier: OrganizationPlanId) -> None:
    await db.execute("UPDATE tenants SET tier=%(tier)s WHERE id=%(tenant)s", {"tier": tier.value, "tenant": tenant_id})
