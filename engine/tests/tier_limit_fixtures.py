"""Link a tenant to an approved organization admission in a live PostgreSQL schema."""

from uuid import UUID, uuid4

from app.db.connection import DbConnection
from app.db.types import as_jsonb
from app.records.organization_plan import OrganizationPlanId
from app.services.organization_plans import plan_for


async def approve_admission_for(db: DbConnection, tenant_id: UUID, tier: OrganizationPlanId) -> UUID:
    """Insert an approved request that provisioned ``tenant_id``."""
    request_id = uuid4()
    await db.execute(
        "INSERT INTO organization_admission_requests "
        "(id,organization_name,email,tier,plan,status,reviewed_at,provisioned_at,provisioned_tenant_id) "
        "VALUES (%(id)s,'Linked',%(email)s,%(tier)s,%(plan)s,'approved',now(),now(),%(tenant)s)",
        {
            "id": request_id,
            "email": f"{request_id.hex}@example.test",
            "tier": tier.value,
            "plan": as_jsonb(plan_for(tier).model_dump(mode="json")),
            "tenant": tenant_id,
        },
    )
    return request_id
