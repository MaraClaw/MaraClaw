"""Durable, independently leased gateway report destinations."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import (
    datetime_from_row,
    int_from_row,
    uuid_from_row,
    uuid_from_row_opt,
)
from app.records.outbound_plan import OutboundPlan

type DestinationKind = Literal["im", "agent"]
type TerminalReason = Literal[
    "source_agent_unavailable",
    "source_tenant_changed",
    "destination_agent_unavailable",
    "destination_tenant_changed",
    "destination_relationship_changed",
    "destination_session_unavailable",
    "payload_rejected",
    "whatsapp_window_unavailable",
    "whatsapp_window_expired",
]
_destination_kind = TypeAdapter(DestinationKind)
_terminal_reason = TypeAdapter(TerminalReason)


@dataclass(slots=True)
class GatewayReportDeliveryRecord:
    id: UUID
    message_id: UUID
    destination_kind: DestinationKind
    destination_id: UUID
    tenant_id: UUID | None = None
    available_at: datetime | None = None
    attempts: int = 0
    claim_token: UUID | None = None
    lease_expires_at: datetime | None = None
    delivered_at: datetime | None = None
    terminal_reason: TerminalReason | None = None
    terminal_at: datetime | None = None
    im_plan: OutboundPlan | None = None
    next_chunk: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> GatewayReportDeliveryRecord:
        terminal_reason = row.get("terminal_reason")
        im_plan = row.get("im_plan")
        return cls(
            id=uuid_from_row(row["id"]),
            message_id=uuid_from_row(row["message_id"]),
            destination_kind=_destination_kind.validate_python(row["destination_kind"]),
            destination_id=uuid_from_row(row["destination_id"]),
            tenant_id=uuid_from_row_opt(row.get("tenant_id")),
            available_at=datetime_from_row(row.get("available_at")),
            attempts=int_from_row(row.get("attempts")),
            claim_token=uuid_from_row_opt(row.get("claim_token")),
            lease_expires_at=datetime_from_row(row.get("lease_expires_at")),
            delivered_at=datetime_from_row(row.get("delivered_at")),
            terminal_reason=_terminal_reason.validate_python(terminal_reason) if terminal_reason is not None else None,
            terminal_at=datetime_from_row(row.get("terminal_at")),
            im_plan=OutboundPlan.model_validate(im_plan) if im_plan is not None else None,
            next_chunk=int_from_row(row.get("next_chunk")),
        )
