from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row, uuid_from_row_opt


class AdmissionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class AdmissionMethod(StrEnum):
    INVITATION = "invitation"
    VERIFIED_DOMAIN = "verified_domain"
    SSO = "sso"


@dataclass(slots=True)
class CompanyAdmissionRecord:
    id: UUID
    applicant_user_id: UUID
    target_tenant_id: UUID
    method: AdmissionMethod
    created_at: datetime
    status: AdmissionStatus = AdmissionStatus.PENDING
    source_tenant_id: UUID | None = None
    invitation_code: str | None = None
    decided_at: datetime | None = None
    decided_by_user_id: UUID | None = None
    decision_reason: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CompanyAdmissionRecord:
        created_at = datetime_from_row(row["created_at"])
        if created_at is None:
            raise ValueError("Admission creation timestamp is required")
        return cls(
            id=uuid_from_row(row["id"]),
            applicant_user_id=uuid_from_row(row["applicant_user_id"]),
            target_tenant_id=uuid_from_row(row["target_tenant_id"]),
            source_tenant_id=uuid_from_row_opt(row.get("source_tenant_id")),
            method=AdmissionMethod(str_from_row(row["method"])),
            status=AdmissionStatus(str_from_row(row["status"])),
            invitation_code=str_from_row(row.get("invitation_code")) or None,
            created_at=created_at,
            decided_at=datetime_from_row(row.get("decided_at")),
            decided_by_user_id=uuid_from_row_opt(row.get("decided_by_user_id")),
            decision_reason=str_from_row(row.get("decision_reason")) or None,
        )
