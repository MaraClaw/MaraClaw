from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.domain.approvals import ApprovalStatus
from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    str_from_row,
    str_from_row_opt,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.records.capacity_order import CapacityEvidence, CapacityRecipient
from app.records.organization_plan import OrganizationBillingCycle, OrganizationPlan, OrganizationPlanId

ADMISSION_REFERENCE_PREFIX = "MO"


@dataclass(frozen=True, slots=True)
class AdmissionPayment:
    """The quoted payment of a request: billing cycle, USD and VND amounts, the rate and the bank recipient."""

    billing: OrganizationBillingCycle
    amount_usd: int
    usd_vnd_rate: int
    amount_vnd: int
    recipient: CapacityRecipient
    reference: str


@dataclass(slots=True)
class OrganizationAdmissionRecord:
    id: UUID
    email: str
    phone: str
    tier: OrganizationPlanId
    plan: OrganizationPlan
    billing: OrganizationBillingCycle
    amount_usd: int
    usd_vnd_rate: int
    amount_vnd: int
    recipient: CapacityRecipient
    payment_reference: str
    evidence: CapacityEvidence
    created_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewed_at: datetime | None = None
    reviewed_by: UUID | None = None
    review_note: str | None = None
    organization_name: str | None = None
    provisioned_at: datetime | None = None
    provisioned_tenant_id: UUID | None = None
    provisioned_admin_user_id: UUID | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> OrganizationAdmissionRecord:
        created_at = datetime_from_row(row["created_at"])
        if created_at is None:
            raise ValueError("Organization admission creation timestamp is required")
        return cls(
            id=uuid_from_row(row["id"]),
            email=str_from_row(row["email"]),
            phone=str_from_row(row["phone"]),
            tier=OrganizationPlanId(str_from_row(row["tier"])),
            plan=OrganizationPlan.model_validate(row["plan"]),
            billing=OrganizationBillingCycle(str_from_row(row["billing"])),
            amount_usd=int_from_row(row["amount_usd"]),
            usd_vnd_rate=int_from_row(row["usd_vnd_rate"]),
            amount_vnd=int_from_row(row["amount_vnd"]),
            recipient=CapacityRecipient.from_json(row["recipient"]),
            payment_reference=str_from_row(row["payment_reference"]),
            evidence=CapacityEvidence(
                storage_key=str_from_row(row["evidence_storage_key"]),
                content_type=str_from_row(row["evidence_content_type"]),
                filename=str_from_row(row["evidence_filename"]),
                size_bytes=int_from_row(row["evidence_size_bytes"]),
                sha256=str_from_row(row["evidence_sha256"]),
            ),
            created_at=created_at,
            status=ApprovalStatus(str_from_row(row["status"])),
            reviewed_at=datetime_from_row(row.get("reviewed_at")),
            reviewed_by=uuid_from_row_opt(row.get("reviewed_by")),
            review_note=str_from_row_opt(row.get("review_note")),
            organization_name=str_from_row_opt(row.get("organization_name")),
            provisioned_at=datetime_from_row(row.get("provisioned_at")),
            provisioned_tenant_id=uuid_from_row_opt(row.get("provisioned_tenant_id")),
            provisioned_admin_user_id=uuid_from_row_opt(row.get("provisioned_admin_user_id")),
        )
