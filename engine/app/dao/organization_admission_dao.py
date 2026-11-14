from typing import ClassVar, final
from uuid import UUID

from app.core.domain.approvals import ApprovalStatus
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.capacity_order import CapacityEvidence
from app.records.organization_admission import AdmissionPayment, OrganizationAdmissionRecord
from app.records.organization_plan import OrganizationPlan

# The constraint behind ``payment_reference UNIQUE`` in the baseline (PostgreSQL's default name for it).
REFERENCE_UNIQUE_CONSTRAINT = "organization_admission_requests_payment_reference_key"


@final
class OrganizationAdmissionDAO(BaseDAO[OrganizationAdmissionRecord]):
    table: ClassVar[str] = "organization_admission_requests"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "email",
        "phone",
        "tier",
        "plan",
        "billing",
        "amount_usd",
        "usd_vnd_rate",
        "amount_vnd",
        "recipient",
        "payment_reference",
        "evidence_storage_key",
        "evidence_content_type",
        "evidence_filename",
        "evidence_size_bytes",
        "evidence_sha256",
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
        self,
        *,
        request_id: UUID,
        email: str,
        phone: str,
        organization_name: str,
        plan: OrganizationPlan,
        payment: AdmissionPayment,
        evidence: CapacityEvidence,
    ) -> OrganizationAdmissionRecord | None:
        """Insert a pending request with its payment snapshot and evidence.

        ``None`` when this normalized email already has a pending request (the original is preserved). A reused
        ``payment_reference`` raises ``UniqueViolationError`` on ``REFERENCE_UNIQUE_CONSTRAINT``.
        """
        async with self.session() as db:
            row = await db.fetchone(
                f"INSERT INTO {self.table} (id,organization_name,email,phone,tier,plan,billing,amount_usd,"
                "usd_vnd_rate,amount_vnd,recipient,payment_reference,evidence_storage_key,evidence_content_type,"
                "evidence_filename,evidence_size_bytes,evidence_sha256) "
                "VALUES (%(id)s,%(name)s,%(email)s,%(phone)s,%(tier)s,%(plan)s,%(billing)s,%(amount_usd)s,"
                "%(usd_vnd_rate)s,%(amount_vnd)s,%(recipient)s,%(reference)s,%(storage_key)s,%(content_type)s,"
                "%(filename)s,%(size_bytes)s,%(sha256)s) "
                f"ON CONFLICT (email) WHERE status='pending' DO NOTHING RETURNING {self._select_list()}",
                {
                    "id": request_id,
                    "name": organization_name,
                    "email": email,
                    "phone": phone,
                    "tier": plan.id.value,
                    "plan": as_jsonb(plan.model_dump(mode="json")),
                    "billing": payment.billing.value,
                    "amount_usd": payment.amount_usd,
                    "usd_vnd_rate": payment.usd_vnd_rate,
                    "amount_vnd": payment.amount_vnd,
                    "recipient": as_jsonb(payment.recipient.as_json()),
                    "reference": payment.reference,
                    "storage_key": evidence.storage_key,
                    "content_type": evidence.content_type,
                    "filename": evidence.filename,
                    "size_bytes": evidence.size_bytes,
                    "sha256": evidence.sha256,
                },
            )
            return self.record_factory(row) if row is not None else None

    async def has_pending(self, email: str) -> bool:
        async with self.session() as db:
            return bool(
                await db.fetchval(
                    f"SELECT EXISTS (SELECT 1 FROM {self.table} WHERE email=%(email)s AND status='pending')",
                    {"email": email},
                )
            )

    async def get_locked(self, request_id: UUID) -> OrganizationAdmissionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} WHERE id=%(id)s FOR UPDATE", {"id": request_id}
            )
            return self.record_factory(row) if row is not None else None

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
