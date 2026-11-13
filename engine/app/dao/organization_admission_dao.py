from typing import ClassVar, final
from uuid import UUID, uuid4

from app.core.domain.approvals import ApprovalStatus
from app.core.json_types import str_from_row
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.organization_admission import LinkedAdmissionPlan, OrganizationAdmissionRecord
from app.records.organization_plan import OrganizationPlan, OrganizationPlanId


@final
class OrganizationAdmissionDAO(BaseDAO[OrganizationAdmissionRecord]):
    table: ClassVar[str] = "organization_admission_requests"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "email",
        "tier",
        "plan",
        "status",
        "created_at",
        "reviewed_at",
        "reviewed_by",
        "review_note",
        "organization_name",
        "provisioned_at",
        "provisioned_tenant_id",
        "provisioned_admin_user_id",
    )
    record_factory = staticmethod(OrganizationAdmissionRecord.from_row)

    async def insert_pending(
        self, *, email: str, organization_name: str, plan: OrganizationPlan
    ) -> OrganizationAdmissionRecord | None:
        """Insert a pending request; ``None`` when this normalized email already has one (original is preserved)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"INSERT INTO {self.table} (id,organization_name,email,tier,plan) "
                "VALUES (%(id)s,%(name)s,%(email)s,%(tier)s,%(plan)s) "
                f"ON CONFLICT (email) WHERE status='pending' DO NOTHING RETURNING {self._select_list()}",
                {
                    "id": uuid4(),
                    "name": organization_name,
                    "email": email,
                    "tier": plan.id.value,
                    "plan": as_jsonb(plan.model_dump(mode="json")),
                },
            )
            return self.record_factory(row) if row is not None else None

    async def get_locked(self, request_id: UUID) -> OrganizationAdmissionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} WHERE id=%(id)s FOR UPDATE", {"id": request_id}
            )
            return self.record_factory(row) if row is not None else None

    async def approved_tiers_for_tenant(self, tenant_id: UUID) -> list[OrganizationPlanId]:
        """Tiers of approved requests that provisioned ``tenant_id``: at most two rows, so a caller can detect a double link."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT tier FROM {self.table} WHERE status='approved' AND provisioned_tenant_id=%(tenant)s "
                "ORDER BY provisioned_at,id LIMIT 2",
                {"tenant": tenant_id},
            )
            return [OrganizationPlanId(str_from_row(row["tier"])) for row in rows]

    async def approved_plans_for_tenant(self, tenant_id: UUID) -> list[LinkedAdmissionPlan]:
        """Raw ``id, tier, plan`` of approved requests that provisioned ``tenant_id`` (same order and two-row cap as
        ``approved_tiers_for_tenant``). It skips ``OrganizationAdmissionRecord`` validation so an outdated snapshot
        can still be read and refreshed."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT id, tier, plan FROM {self.table} WHERE status='approved' AND provisioned_tenant_id=%(tenant)s "
                "ORDER BY provisioned_at,id LIMIT 2",
                {"tenant": tenant_id},
            )
            return [LinkedAdmissionPlan.from_row(row) for row in rows]

    async def refresh_tier_and_plan(
        self, request_id: UUID, *, tier: OrganizationPlanId, plan: OrganizationPlan
    ) -> None:
        async with self.session() as db:
            await db.execute(
                f"UPDATE {self.table} SET tier=%(tier)s, plan=%(plan)s WHERE id=%(id)s",
                {"id": request_id, "tier": tier.value, "plan": as_jsonb(plan.model_dump(mode="json"))},
            )

    async def list_by_status(
        self, status: ApprovalStatus, *, limit: int, offset: int
    ) -> list[OrganizationAdmissionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} WHERE status=%(status)s "
                "ORDER BY created_at DESC,id DESC LIMIT %(limit)s OFFSET %(offset)s",
                {"status": status.value, "limit": limit, "offset": offset},
            )
            return [self.record_factory(row) for row in rows]


organization_admission_dao = OrganizationAdmissionDAO()
