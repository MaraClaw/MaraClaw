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

type DestinationKind = Literal["im", "agent"]
_destination_kind = TypeAdapter(DestinationKind)


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

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> GatewayReportDeliveryRecord:
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
        )
