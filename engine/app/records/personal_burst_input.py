"""Receipt-keyed Personal burst outcomes: accepted members and terminal rejections (no rejected body kept)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import TypeAdapter

from app.core.json_types import str_from_row, uuid_from_row, uuid_from_row_opt
from app.records.personal_burst import required_datetime

BurstOutcome = Literal["accepted", "capacity_rejected", "oversize_rejected", "quota_rejected"]
RejectedOutcome = Literal["capacity_rejected", "oversize_rejected", "quota_rejected"]
_OUTCOME: Final = TypeAdapter(BurstOutcome)


@dataclass(frozen=True, slots=True)
class ReceiptKey:
    """Identity of ``channel_event_receipts``: the single native-event reserve/uniqueness authority."""

    config_id: UUID
    provider: str
    event_key: str


@dataclass(frozen=True, slots=True)
class NewBurstMember:
    """One newly accepted original. ``chat_message_id`` is its already-persisted transcript row."""

    receipt: ReceiptKey
    text: str
    chat_message_id: UUID | None

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode())


@dataclass(frozen=True, slots=True)
class PersonalBurstInput:
    id: UUID
    receipt: ReceiptKey
    outcome: BurstOutcome
    created_at: datetime
    gateway_id: UUID | None = None
    ordinal: int | None = None
    text: str | None = None
    text_bytes: int | None = None
    chat_message_id: UUID | None = None
    notice_id: UUID | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> PersonalBurstInput:
        ordinal, text_bytes, text = row["ordinal"], row["text_bytes"], row["text"]
        return cls(
            id=uuid_from_row(row["id"]),
            receipt=ReceiptKey(
                uuid_from_row(row["config_id"]), str_from_row(row["provider"]), str_from_row(row["event_key"])
            ),
            outcome=_OUTCOME.validate_python(row["outcome"]),
            created_at=required_datetime(row["created_at"]),
            gateway_id=uuid_from_row_opt(row["batch_id"]),
            ordinal=ordinal if isinstance(ordinal, int) else None,
            text=text if isinstance(text, str) else None,
            text_bytes=text_bytes if isinstance(text_bytes, int) else None,
            chat_message_id=uuid_from_row_opt(row["chat_message_id"]),
            notice_id=uuid_from_row_opt(row["notice_id"]),
        )
