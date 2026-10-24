from datetime import datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.records.company_admission import AdmissionMethod, AdmissionStatus


class AdmissionTenant(BaseModel):
    id: UUID
    name: str


class AdmissionApplicant(BaseModel):
    id: UUID
    email: str | None


class RequestSummary(BaseModel):
    id: UUID
    status: AdmissionStatus
    source_tenant_id: UUID | None
    target_tenant: AdmissionTenant
    method: AdmissionMethod
    created_at: datetime
    decided_at: datetime | None
    decision_reason: str | None


class RequestDetail(RequestSummary):
    applicant: AdmissionApplicant
    source_tenant: AdmissionTenant | None
    decided_by_user_id: UUID | None


class ApplicantPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[RequestSummary]
    next_cursor: str | None


class ReviewPage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    items: list[RequestDetail]
    next_cursor: str | None


class PendingAdmissionResponse(BaseModel):
    status: Literal["pending"] = "pending"
    request: RequestSummary


class AdmissionDecision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)
