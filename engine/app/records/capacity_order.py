"""Purchase and renewal orders for paid extra accounts and agents (``capacity_orders``)."""

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

from app.core.json_types import (
    JsonObject,
    date_from_row,
    date_from_row_opt,
    datetime_from_row,
    int_from_row,
    mapping_from_row,
    str_from_row,
    str_from_row_opt,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.records.organization_plan import OrganizationPlanId

REFERENCE_PREFIX: Final = "MC"
REFERENCE_LENGTH: Final = 8
MAX_EXTRAS_PER_KIND: Final = 10_000
MAX_SETTING_TEXT_LENGTH: Final = 120
MAX_USD_VND_RATE: Final = 1_000_000
_BASE32_ALPHABET: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class CapacityOrderKind(StrEnum):
    PURCHASE = "purchase"
    RENEWAL = "renewal"


class CapacityOrderStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def new_reference() -> str:
    """A fresh transfer-note code: ``MC`` plus eight uppercase base32 characters."""
    return REFERENCE_PREFIX + "".join(secrets.choice(_BASE32_ALPHABET) for _ in range(REFERENCE_LENGTH))


@dataclass(frozen=True, slots=True)
class CapacityRecipient:
    """The bank account the company pays into, snapshotted on the order when it was created."""

    bank_name: str
    account_number: str
    branch_name: str

    @classmethod
    def from_json(cls, value: object) -> CapacityRecipient:
        data = mapping_from_row(value)
        return cls(
            bank_name=str_from_row(data.get("bank_name")),
            account_number=str_from_row(data.get("account_number")),
            branch_name=str_from_row(data.get("branch_name")),
        )

    def as_json(self) -> JsonObject:
        return {"bank_name": self.bank_name, "account_number": self.account_number, "branch_name": self.branch_name}


@dataclass(frozen=True, slots=True)
class CapacityEvidence:
    storage_key: str
    content_type: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class NewCapacityOrder:
    """Everything an order needs at creation; the reference and status are assigned on insert."""

    tenant_id: UUID
    kind: CapacityOrderKind
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
    recipient: CapacityRecipient
    created_by: UUID | None = None
    grace_until: date | None = None
    source_order_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class CapacityOrderRecord:
    id: UUID
    tenant_id: UUID
    reference: str
    kind: CapacityOrderKind
    status: CapacityOrderStatus
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
    recipient: CapacityRecipient
    created_at: datetime
    source_order_id: UUID | None = None
    grace_until: date | None = None
    evidence: CapacityEvidence | None = None
    created_by: UUID | None = None
    submitted_by: UUID | None = None
    submitted_at: datetime | None = None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> CapacityOrderRecord:
        created_at = datetime_from_row(row["created_at"])
        if created_at is None:
            raise ValueError("Capacity order creation timestamp is required")
        storage_key = str_from_row_opt(row.get("evidence_storage_key"))
        evidence = (
            CapacityEvidence(
                storage_key=storage_key,
                content_type=str_from_row(row.get("evidence_content_type")),
                filename=str_from_row(row.get("evidence_filename")),
                size_bytes=int_from_row(row.get("evidence_size_bytes")),
                sha256=str_from_row(row.get("evidence_sha256")),
            )
            if storage_key is not None
            else None
        )
        return cls(
            id=uuid_from_row(row["id"]),
            tenant_id=uuid_from_row(row["tenant_id"]),
            reference=str_from_row(row["reference"]),
            kind=CapacityOrderKind(str_from_row(row["kind"])),
            status=CapacityOrderStatus(str_from_row(row["status"])),
            tier=OrganizationPlanId(str_from_row(row["tier"])),
            billing_month=date_from_row(row["billing_month"]),
            extra_accounts=int_from_row(row["extra_accounts"]),
            extra_agents=int_from_row(row["extra_agents"]),
            account_unit_usd=int_from_row(row["account_unit_usd"]),
            agent_unit_usd=int_from_row(row["agent_unit_usd"]),
            days_charged=int_from_row(row["days_charged"]),
            days_in_month=int_from_row(row["days_in_month"]),
            amount_usd_cents=int_from_row(row["amount_usd_cents"]),
            usd_vnd_rate=int_from_row(row["usd_vnd_rate"]),
            amount_vnd=int_from_row(row["amount_vnd"]),
            recipient=CapacityRecipient.from_json(row["recipient"]),
            created_at=created_at,
            source_order_id=uuid_from_row_opt(row.get("source_order_id")),
            grace_until=date_from_row_opt(row.get("grace_until")),
            evidence=evidence,
            created_by=uuid_from_row_opt(row.get("created_by")),
            submitted_by=uuid_from_row_opt(row.get("submitted_by")),
            submitted_at=datetime_from_row(row.get("submitted_at")),
            reviewed_by=uuid_from_row_opt(row.get("reviewed_by")),
            reviewed_at=datetime_from_row(row.get("reviewed_at")),
            review_note=str_from_row_opt(row.get("review_note")),
        )
