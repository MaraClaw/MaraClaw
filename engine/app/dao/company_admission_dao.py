from datetime import datetime
from typing import ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.company_admission import AdmissionStatus, CompanyAdmissionRecord
from app.records.tenant import TenantRecord


@final
class CompanyAdmissionDAO(BaseDAO[CompanyAdmissionRecord]):
    table: ClassVar[str] = "company_admission_requests"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "applicant_user_id",
        "target_tenant_id",
        "source_tenant_id",
        "method",
        "status",
        "invitation_code",
        "created_at",
        "decided_at",
        "decided_by_user_id",
        "decision_reason",
    )
    record_factory = staticmethod(CompanyAdmissionRecord.from_row)

    async def get_locked(self, request_id: UUID) -> CompanyAdmissionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT * FROM company_admission_requests WHERE id=%(id)s FOR UPDATE", {"id": request_id}
            )
            return self.record_factory(row) if row is not None else None

    async def pending_for(self, applicant_id: UUID) -> CompanyAdmissionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT * FROM company_admission_requests WHERE applicant_user_id=%(id)s AND status='pending'",
                {"id": applicant_id},
            )
            return self.record_factory(row) if row is not None else None

    async def tenant(self, tenant_id: UUID) -> TenantRecord | None:
        async with self.session() as db:
            row = await db.fetchone("SELECT * FROM tenants WHERE id=%(id)s", {"id": tenant_id})
            return TenantRecord.from_row(row) if row is not None else None

    async def list_mine(self, applicant_id: UUID, *, limit: int, offset: int) -> list[CompanyAdmissionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT * FROM company_admission_requests WHERE applicant_user_id=%(id)s "
                "ORDER BY created_at DESC,id DESC LIMIT %(limit)s OFFSET %(offset)s",
                {"id": applicant_id, "limit": limit, "offset": offset},
            )
            return [self.record_factory(row) for row in rows]

    async def list_review(
        self, tenant_id: UUID, status: AdmissionStatus, *, limit: int, offset: int
    ) -> list[CompanyAdmissionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT * FROM company_admission_requests WHERE target_tenant_id=%(id)s AND status=%(status)s "
                "ORDER BY created_at DESC,id DESC LIMIT %(limit)s OFFSET %(offset)s",
                {"id": tenant_id, "status": status.value, "limit": limit, "offset": offset},
            )
            return [self.record_factory(row) for row in rows]

    async def list_review_page(
        self,
        tenant_id: UUID,
        status: AdmissionStatus,
        *,
        limit: int,
        before: tuple[datetime, UUID] | None = None,
    ) -> list[CompanyAdmissionRecord]:
        params: dict[str, object] = {"tenant_id": tenant_id, "status": status.value, "limit": limit + 1}
        boundary = ""
        if before is not None:
            boundary = " AND (created_at,id) < (%(created_at)s,%(request_id)s)"
            params.update(created_at=before[0], request_id=before[1])
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT * FROM company_admission_requests "
                "WHERE target_tenant_id=%(tenant_id)s AND status=%(status)s"
                + boundary
                + " ORDER BY created_at DESC,id DESC LIMIT %(limit)s",
                params,
            )
            return [self.record_factory(row) for row in rows]


company_admission_dao = CompanyAdmissionDAO()
