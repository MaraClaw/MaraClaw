"""Native provider acceptance identities, retained for the connector lifetime."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


@dataclass(frozen=True, slots=True)
class ChannelEventReceiptRecord:
    config_id: UUID
    provider: str
    event_key: str
    agent_id: UUID
    tenant_id: UUID | None
    fingerprint: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChannelEventReceiptRecord:
        return cls(
            config_id=uuid_from_row(row["config_id"]),
            provider=str_from_row(row["provider"]),
            event_key=str_from_row(row["event_key"]),
            agent_id=uuid_from_row(row["agent_id"]),
            tenant_id=uuid_from_row(row["tenant_id"]) if row["tenant_id"] is not None else None,
            fingerprint=str_from_row(row["fingerprint"]),
            created_at=datetime_from_row(row["created_at"]),
        )
