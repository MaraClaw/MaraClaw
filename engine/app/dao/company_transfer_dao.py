from dataclasses import fields
from datetime import datetime
from typing import final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.company_transfer import CompanyTransferRecord, TransferReviewView


@final
class CompanyTransferDAO(BaseDAO[CompanyTransferRecord]):
    table = "company_transfer_requests"
    columns = tuple(field.name for field in fields(CompanyTransferRecord))
    record_factory = staticmethod(CompanyTransferRecord.from_row)

    async def get_locked(self, request_id: UUID) -> CompanyTransferRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT * FROM company_transfer_requests WHERE id=%(id)s FOR UPDATE", {"id": request_id}
            )
            return self.record_factory(row) if row is not None else None

    async def active_for(self, user_id: UUID) -> CompanyTransferRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT t.* FROM company_transfer_requests t LEFT JOIN company_admission_requests a "
                "ON a.id=t.admission_request_id WHERE t.applicant_user_id=%(id)s "
                "AND (t.status='payment_review' OR a.status='pending') ORDER BY t.created_at DESC LIMIT 1",
                {"id": user_id},
            )
            return self.record_factory(row) if row is not None else None

    async def for_admission(self, admission_id: UUID) -> CompanyTransferRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                "SELECT * FROM company_transfer_requests WHERE admission_request_id=%(id)s", {"id": admission_id}
            )
            return self.record_factory(row) if row is not None else None

    async def list_requests(self, applicant_id: UUID | None, limit: int = 100) -> list[CompanyTransferRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT * FROM company_transfer_requests WHERE (%(id)s::uuid IS NULL OR applicant_user_id=%(id)s) "
                "ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
                {"id": applicant_id, "limit": limit},
            )
            return [self.record_factory(row) for row in rows]

    async def list_review_page(
        self, view: TransferReviewView, *, limit: int, before: tuple[datetime, UUID] | None = None
    ) -> list[CompanyTransferRecord]:
        """Return up to ``limit + 1`` paid-choice rows for ``view``, strictly after ``before`` in keyset order."""
        params: dict[str, object] = {"limit": limit + 1}
        predicate = "choice='paid'"
        if view == TransferReviewView.PENDING:
            predicate += " AND status='payment_review'"
        if before is not None:
            predicate += " AND (created_at,id) < (%(created_at)s,%(request_id)s)"
            params.update(created_at=before[0], request_id=before[1])
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT * FROM company_transfer_requests WHERE "
                + predicate
                + " ORDER BY created_at DESC,id DESC LIMIT %(limit)s",
                params,
            )
            return [self.record_factory(row) for row in rows]


company_transfer_dao = CompanyTransferDAO()
