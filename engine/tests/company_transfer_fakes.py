from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.records.company_admission import AdmissionStatus, CompanyAdmissionRecord
from app.records.company_transfer import CompanyTransferRecord, TransferChoice, TransferReviewView, TransferStatus
from app.records.tenant import TenantRecord


class TransferStore:
    def __init__(self, admissions: AdmissionStore) -> None:
        self.rows: dict[UUID, CompanyTransferRecord] = {}
        self.admissions = admissions

    async def create(self, *, obj_in: Mapping[str, object]) -> CompanyTransferRecord:
        row = CompanyTransferRecord.from_row(
            {"id": uuid4(), "created_at": datetime.now(UTC), "status": "payment_review", **obj_in}
        )
        self.rows[row.id] = row
        return row

    async def update(self, *, db_obj: CompanyTransferRecord, obj_in: Mapping[str, object]) -> CompanyTransferRecord:
        row = CompanyTransferRecord.from_row({**asdict(db_obj), **obj_in})
        self.rows[row.id] = row
        return row

    async def get(self, request_id: UUID) -> CompanyTransferRecord | None:
        return self.rows.get(request_id)

    async def get_locked(self, request_id: UUID) -> CompanyTransferRecord | None:
        return await self.get(request_id)

    async def list_requests(self, applicant_id: UUID | None, limit: int = 100) -> list[CompanyTransferRecord]:
        rows = [row for row in self.rows.values() if applicant_id is None or row.applicant_user_id == applicant_id]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return rows[:limit]

    async def active_for(self, user_id: UUID) -> CompanyTransferRecord | None:
        return next(
            (
                row
                for row in self.rows.values()
                if row.applicant_user_id == user_id
                and (
                    row.status == TransferStatus.PAYMENT_REVIEW
                    or (
                        row.admission_request_id is not None
                        and row.admission_request_id in self.admissions.rows
                        and self.admissions.rows[row.admission_request_id].status == AdmissionStatus.PENDING
                    )
                )
            ),
            None,
        )

    async def for_admission(self, admission_id: UUID) -> CompanyTransferRecord | None:
        return next((row for row in self.rows.values() if row.admission_request_id == admission_id), None)

    async def list_member_page(
        self, applicant_id: UUID, *, limit: int, before: tuple[datetime, UUID] | None = None
    ) -> list[CompanyTransferRecord]:
        rows = [
            row
            for row in self.rows.values()
            if row.applicant_user_id == applicant_id and (before is None or (row.created_at, row.id) < before)
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return rows[: limit + 1]

    async def list_review_page(
        self, view: TransferReviewView, *, limit: int, before: tuple[datetime, UUID] | None = None
    ) -> list[CompanyTransferRecord]:
        rows = [
            row
            for row in self.rows.values()
            if row.choice == TransferChoice.PAID
            and (view == TransferReviewView.RECENT or row.status == TransferStatus.PAYMENT_REVIEW)
            and (before is None or (row.created_at, row.id) < before)
        ]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return rows[: limit + 1]


class AdmissionStore:
    def __init__(self, tenants: list[TenantRecord]) -> None:
        self.rows: dict[UUID, CompanyAdmissionRecord] = {}
        self.tenants = {tenant.id: tenant for tenant in tenants}

    async def tenant(self, tenant_id: UUID) -> TenantRecord | None:
        return self.tenants.get(tenant_id)

    async def get(self, request_id: UUID) -> CompanyAdmissionRecord | None:
        return self.rows.get(request_id)

    async def pending_for(self, user_id: UUID) -> CompanyAdmissionRecord | None:
        return next(
            (
                row
                for row in self.rows.values()
                if row.applicant_user_id == user_id and row.status == AdmissionStatus.PENDING
            ),
            None,
        )

    async def create(self, *, obj_in: Mapping[str, object]) -> CompanyAdmissionRecord:
        row = CompanyAdmissionRecord.from_row(
            {"id": uuid4(), "created_at": datetime.now(UTC), "status": "pending", **obj_in}
        )
        self.rows[row.id] = row
        return row
