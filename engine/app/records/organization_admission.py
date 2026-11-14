from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.domain.approvals import ApprovalStatus
from app.core.json_types import datetime_from_row, str_from_row, str_from_row_opt, uuid_from_row, uuid_from_row_opt
from app.records.organization_plan import OrganizationPlan, OrganizationPlanId


@dataclass(slots=True)
class OrganizationAdmissionRecord:
    id: UUID
    email: str
    tier: OrganizationPlanId
    plan: OrganizationPlan
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
            tier=OrganizationPlanId(str_from_row(row["tier"])),
            plan=OrganizationPlan.model_validate(row["plan"]),
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


@dataclass(frozen=True, slots=True)
class LinkedAdmissionPlan:
    """Stored tier and plan snapshot of an approved admission, left unvalidated: an outdated snapshot may not parse."""

    id: UUID
    tier: OrganizationPlanId
    plan: object

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> LinkedAdmissionPlan:
        return cls(id=uuid_from_row(row["id"]), tier=OrganizationPlanId(str_from_row(row["tier"])), plan=row["plan"])
