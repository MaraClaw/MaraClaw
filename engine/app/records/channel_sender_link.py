from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.core.json_types import datetime_from_row, str_from_row, uuid_from_row


@dataclass(frozen=True, slots=True)
class ChannelSenderBinding:
    user_id: UUID
    tenant_id: UUID
    external_user_id: str
    linked_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChannelSenderBinding:
        return cls(
            uuid_from_row(row["user_id"]),
            uuid_from_row(row["tenant_id"]),
            str_from_row(row["external_user_id"]),
            datetime_from_row(row["linked_at"]),
        )


@dataclass(frozen=True, slots=True)
class ChannelLinkCode:
    user_id: UUID
    tenant_id: UUID
    expires_at: datetime
    consumed_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> ChannelLinkCode:
        expires = datetime_from_row(row["expires_at"])
        if expires is None:
            raise ValueError("Link code expiry is required")
        return cls(
            uuid_from_row(row["user_id"]),
            uuid_from_row(row["tenant_id"]),
            expires,
            datetime_from_row(row["consumed_at"]),
        )
