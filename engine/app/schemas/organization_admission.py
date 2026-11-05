import unicodedata
from datetime import datetime
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, field_validator

from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.records.organization_admission_email import CredentialEmailStatus
from app.records.organization_plan import OrganizationPlan, OrganizationPlanId


class OrganizationPlansResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    plans: list[OrganizationPlan]


class OrganizationAdmissionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    organization_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: EmailStr
    tier: OrganizationPlanId

    @field_validator("organization_name")
    @classmethod
    def single_line_text(cls, value: str) -> str:
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Organization name must not contain control characters")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class OrganizationAdmissionAcknowledgment(BaseModel):
    """Generic acknowledgment: identical for new and repeated requests, so it never discloses request state."""

    status: Literal["pending"] = "pending"


class OrganizationAdmissionDecisionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision: ApprovalAction
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None

    @field_validator("note")
    @classmethod
    def blank_note_is_absent(cls, value: str | None) -> str | None:
        return value or None


class OrganizationAdmissionItem(BaseModel):
    id: UUID
    organization_name: str | None
    email: str
    tier: OrganizationPlanId
    plan: OrganizationPlan
    status: ApprovalStatus
    created_at: datetime
    reviewed_at: datetime | None
    reviewed_by: UUID | None
    review_note: str | None
    credential_email_status: CredentialEmailStatus | None
    credential_email_error: str | None


class OrganizationAdmissionPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[OrganizationAdmissionItem]
    has_more: bool
