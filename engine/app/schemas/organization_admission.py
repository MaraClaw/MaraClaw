import unicodedata
from datetime import datetime
from typing import Annotated, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, field_validator

from app.core.domain.approvals import ApprovalAction, ApprovalStatus
from app.records.organization_admission_email import CredentialEmailStatus
from app.records.organization_plan import OrganizationBillingCycle, OrganizationPlan, OrganizationPlanId
from app.schemas.capacity import CapacityRecipientOut
from app.schemas.phone import PhoneNumber


class OrganizationPlansResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    plans: list[OrganizationPlan]


class OrganizationAdmissionQuoteRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    organization_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: EmailStr
    phone: PhoneNumber
    tier: OrganizationPlanId
    billing: OrganizationBillingCycle

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


class OrganizationAdmissionQuoteTerms(BaseModel):
    """Every quote field the signed token binds (``organization_admission_quote_token``); nothing else is trusted."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    organization_name: str
    email: str
    phone: str
    tier: OrganizationPlanId
    billing: OrganizationBillingCycle
    amount_usd: int
    usd_vnd_rate: int
    amount_vnd: int
    recipient: CapacityRecipientOut
    reference: str
    expires_at: datetime


class OrganizationAdmissionQuote(OrganizationAdmissionQuoteTerms):
    """What the visitor pays and where, plus the plan snapshot and the token that submits the request."""

    plan: OrganizationPlan
    quote_token: str


class OrganizationAdmissionAcknowledgment(BaseModel):
    """The request is pending review; ``reference`` is the transfer note its payment was quoted under."""

    status: Literal["pending"]
    reference: str


class OrganizationAdmissionDecisionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    decision: ApprovalAction
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None

    @field_validator("note")
    @classmethod
    def blank_note_is_absent(cls, value: str | None) -> str | None:
        return value or None


class OrganizationAdmissionEvidenceOut(BaseModel):
    """The uploaded payment evidence as the reviewer sees it; the bytes come from the evidence download."""

    filename: str
    content_type: str
    size_bytes: int


class OrganizationAdmissionItem(BaseModel):
    id: UUID
    organization_name: str | None
    email: str
    phone: str
    tier: OrganizationPlanId
    plan: OrganizationPlan
    billing: OrganizationBillingCycle
    amount_usd: int
    usd_vnd_rate: int
    amount_vnd: int
    recipient: CapacityRecipientOut
    payment_reference: str
    evidence: OrganizationAdmissionEvidenceOut
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
