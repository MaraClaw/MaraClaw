"""HTTP models of the capacity order workflow (extra accounts and agents)."""

import unicodedata
from datetime import date, datetime
from typing import Annotated, ClassVar, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.core.domain.approvals import ApprovalAction
from app.records.capacity_order import (
    MAX_EXTRAS_PER_KIND,
    MAX_SETTING_TEXT_LENGTH,
    MAX_USD_VND_RATE,
    CapacityOrderKind,
    CapacityOrderRecord,
    CapacityOrderStatus,
)
from app.records.organization_plan import OrganizationPlanId

SettingText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_SETTING_TEXT_LENGTH)]


class CapacityRecipientOut(BaseModel):
    bank_name: str
    account_number: str
    branch_name: str


class CapacityEvidenceOut(BaseModel):
    content_type: str
    filename: str
    size_bytes: int
    sha256: str


class CapacityOrderOut(BaseModel):
    id: UUID
    tenant_id: UUID
    tenant_name: str | None
    reference: str
    kind: CapacityOrderKind
    status: CapacityOrderStatus
    source_order_id: UUID | None
    tier: OrganizationPlanId
    billing_month: date
    extra_accounts: int
    extra_agents: int
    account_unit_usd: int
    agent_unit_usd: int
    days_charged: int
    days_in_month: int
    amount_usd_cents: int
    usd_vnd_rate: int
    amount_vnd: int
    recipient: CapacityRecipientOut
    grace_until: date | None
    evidence: CapacityEvidenceOut | None
    created_by: UUID | None
    created_at: datetime
    submitted_by: UUID | None
    submitted_at: datetime | None
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_note: str | None

    @classmethod
    def from_record(cls, order: CapacityOrderRecord, tenant_name: str | None = None) -> Self:
        evidence = order.evidence
        return cls(
            id=order.id,
            tenant_id=order.tenant_id,
            tenant_name=tenant_name,
            reference=order.reference,
            kind=order.kind,
            status=order.status,
            source_order_id=order.source_order_id,
            tier=order.tier,
            billing_month=order.billing_month,
            extra_accounts=order.extra_accounts,
            extra_agents=order.extra_agents,
            account_unit_usd=order.account_unit_usd,
            agent_unit_usd=order.agent_unit_usd,
            days_charged=order.days_charged,
            days_in_month=order.days_in_month,
            amount_usd_cents=order.amount_usd_cents,
            usd_vnd_rate=order.usd_vnd_rate,
            amount_vnd=order.amount_vnd,
            recipient=CapacityRecipientOut(
                bank_name=order.recipient.bank_name,
                account_number=order.recipient.account_number,
                branch_name=order.recipient.branch_name,
            ),
            grace_until=order.grace_until,
            evidence=CapacityEvidenceOut(
                content_type=evidence.content_type,
                filename=evidence.filename,
                size_bytes=evidence.size_bytes,
                sha256=evidence.sha256,
            )
            if evidence is not None
            else None,
            created_by=order.created_by,
            created_at=order.created_at,
            submitted_by=order.submitted_by,
            submitted_at=order.submitted_at,
            reviewed_by=order.reviewed_by,
            reviewed_at=order.reviewed_at,
            review_note=order.review_note,
        )


class CapacityOrderPage(BaseModel):
    items: list[CapacityOrderOut]
    has_more: bool


class CapacityOverviewOut(BaseModel):
    """What the Users page needs: caps and usage, the unit prices and the clock a quote is computed on."""

    tenant_id: UUID
    capped: bool
    tier: OrganizationPlanId | None
    included_accounts: int | None
    included_agents: int | None
    extra_accounts: int
    extra_agents: int
    account_limit: int | None
    agent_limit: int | None
    accounts_used: int
    agents_used: int
    account_unit_usd: int | None
    agent_unit_usd: int | None
    today: date
    days_in_month: int
    remaining_days: int
    payments_configured: bool
    usd_vnd_rate: int | None
    open_orders: list[CapacityOrderOut]
    current_renewal: CapacityOrderOut | None


class CapacityOrderCreateRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    extra_accounts: int = Field(default=0, ge=0, le=MAX_EXTRAS_PER_KIND, strict=True)
    extra_agents: int = Field(default=0, ge=0, le=MAX_EXTRAS_PER_KIND, strict=True)

    @model_validator(mode="after")
    def at_least_one_extra(self) -> Self:
        if self.extra_accounts + self.extra_agents == 0:
            raise ValueError("Order at least one extra account or agent")
        return self


class CapacityDecisionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    action: ApprovalAction
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None

    @field_validator("note")
    @classmethod
    def blank_note_is_absent(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def rejection_needs_a_note(self) -> Self:
        if self.action is ApprovalAction.REJECT and self.note is None:
            raise ValueError("A rejection needs a note")
        return self


class CapacityPaymentSettingsRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    bank_name: SettingText
    account_number: SettingText
    branch_name: SettingText
    usd_vnd_rate: int = Field(ge=1, le=MAX_USD_VND_RATE, strict=True)

    @field_validator("bank_name", "account_number", "branch_name")
    @classmethod
    def single_line_text(cls, value: str) -> str:
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("Must not contain control characters")
        return value


class CapacityPaymentSettingsOut(BaseModel):
    """``configured`` is false and the rest null until the genesis platform admin saves them."""

    configured: bool
    bank_name: str | None
    account_number: str | None
    branch_name: str | None
    usd_vnd_rate: int | None
