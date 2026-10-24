from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.json_types import datetime_from_row, int_from_row, str_from_row, uuid_from_row, uuid_from_row_opt
from app.records.company_admission import AdmissionMethod


class TransferChoice(StrEnum):
    PAID = "paid"
    FREE = "free"


class TransferStatus(StrEnum):
    PAYMENT_REVIEW = "payment_review"
    ADMISSION_CREATED = "admission_created"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class CompanyTransferRecord:
    id: UUID
    applicant_user_id: UUID
    source_tenant_id: UUID
    target_tenant_id: UUID
    choice: TransferChoice
    method: AdmissionMethod
    fee_amount_minor: int
    currency: str
    instructions: str
    created_at: datetime
    status: TransferStatus = TransferStatus.PAYMENT_REVIEW
    invitation_code: str | None = None
    free_consent_at: datetime | None = None
    payment_confirmed_at: datetime | None = None
    reviewed_at: datetime | None = None
    reviewed_by_user_id: UUID | None = None
    review_reason: str | None = None
    admission_request_id: UUID | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CompanyTransferRecord:
        created_at = datetime_from_row(row["created_at"])
        if created_at is None:
            raise ValueError("Transfer creation timestamp is required")
        return cls(
            id=uuid_from_row(row["id"]),
            applicant_user_id=uuid_from_row(row["applicant_user_id"]),
            source_tenant_id=uuid_from_row(row["source_tenant_id"]),
            target_tenant_id=uuid_from_row(row["target_tenant_id"]),
            choice=TransferChoice(str_from_row(row["choice"])),
            method=AdmissionMethod(str_from_row(row["method"])),
            fee_amount_minor=int_from_row(row["fee_amount_minor"]),
            currency=str_from_row(row["currency"]),
            instructions=str_from_row(row["instructions"]),
            created_at=created_at,
            status=TransferStatus(str_from_row(row["status"])),
            invitation_code=str_from_row(row.get("invitation_code")) or None,
            free_consent_at=datetime_from_row(row.get("free_consent_at")),
            payment_confirmed_at=datetime_from_row(row.get("payment_confirmed_at")),
            reviewed_at=datetime_from_row(row.get("reviewed_at")),
            reviewed_by_user_id=uuid_from_row_opt(row.get("reviewed_by_user_id")),
            review_reason=str_from_row(row.get("review_reason")) or None,
            admission_request_id=uuid_from_row_opt(row.get("admission_request_id")),
        )
