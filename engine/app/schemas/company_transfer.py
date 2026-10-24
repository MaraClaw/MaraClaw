from datetime import datetime
from typing import Literal, final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.records.company_transfer import TransferChoice, TransferStatus


@final
class TransferQuote(BaseModel):
    model_config = ConfigDict(frozen=True)
    fee_amount_minor: int
    currency: str
    instructions: str
    source_eligible: bool
    has_owned_agents: bool
    explicit_choice_required: bool
    free_requires_destructive_consent: Literal[True] = True


@final
class TransferDisposition(BaseModel):
    model_config = ConfigDict(frozen=True)
    transfer_request_id: UUID
    choice: TransferChoice
    fee_amount_minor: int
    currency: str
    free_consent_at: datetime | None


@final
class TransferSummary(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)
    id: UUID
    applicant_user_id: UUID
    source_tenant_id: UUID
    target_tenant_id: UUID
    choice: TransferChoice
    status: TransferStatus
    fee_amount_minor: int
    currency: str
    instructions: str
    created_at: datetime
    free_consent_at: datetime | None
    payment_confirmed_at: datetime | None
    reviewed_at: datetime | None
    reviewed_by_user_id: UUID | None
    review_reason: str | None
    admission_request_id: UUID | None


@final
class PaymentReviewResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["payment_review"] = "payment_review"
    transfer_request: TransferSummary


@final
class PaymentDecision(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)
