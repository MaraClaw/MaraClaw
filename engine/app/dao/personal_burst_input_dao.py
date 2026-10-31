"""Receipt-keyed Personal burst outcomes."""

from typing import ClassVar, final
from uuid import UUID, uuid4

from app.dao.base import BaseDAO
from app.dao.personal_burst_sql import require_transaction
from app.records.personal_burst_input import (
    NewBurstMember,
    PersonalBurstInput,
    ReceiptKey,
    RejectedOutcome,
)

_COLUMNS = (
    "id",
    "config_id",
    "provider",
    "event_key",
    "outcome",
    "batch_id",
    "ordinal",
    "text",
    "text_bytes",
    "chat_message_id",
    "notice_id",
    "created_at",
)
_BY_RECEIPT = "config_id = %(config_id)s AND provider = %(provider)s AND event_key = %(event_key)s"


def _key(receipt: ReceiptKey) -> dict[str, object]:
    return {"config_id": receipt.config_id, "provider": receipt.provider, "event_key": receipt.event_key}


@final
class PersonalBurstInputDAO(BaseDAO[PersonalBurstInput]):
    table: ClassVar[str] = "personal_burst_inputs"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(PersonalBurstInput.from_row)

    async def find_outcome(self, receipt: ReceiptKey) -> PersonalBurstInput | None:
        """Stored side outcome for a reserved receipt; ``None`` means legacy-accepted (no backfill)."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} WHERE {_BY_RECEIPT}", _key(receipt)
            )
        return self.record_factory(row) if row is not None else None

    async def members(self, gateway_id: UUID) -> list[PersonalBurstInput]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} WHERE batch_id = %(id)s ORDER BY ordinal",
                {"id": gateway_id},
            )
        return [self.record_factory(row) for row in rows]

    async def insert_member(self, gateway_id: UUID, ordinal: int, member: NewBurstMember) -> PersonalBurstInput:
        db = require_transaction("member insert")
        row = await db.fetchone(
            f"INSERT INTO {self.table} (id, config_id, provider, event_key, outcome, batch_id, ordinal, text, "
            "text_bytes, chat_message_id) VALUES (%(id)s, %(config_id)s, %(provider)s, %(event_key)s, 'accepted', "
            "%(batch)s, %(ordinal)s, %(text)s, %(bytes)s, %(message)s) "
            f"RETURNING {self._select_list()}",
            {
                "id": uuid4(),
                **_key(member.receipt),
                "batch": gateway_id,
                "ordinal": ordinal,
                "text": member.text,
                "bytes": member.text_bytes,
                "message": member.chat_message_id,
            },
        )
        if row is None:
            raise RuntimeError("INSERT into personal_burst_inputs returned no row")
        return self.record_factory(row)

    async def record_rejection(
        self, receipt: ReceiptKey, outcome: RejectedOutcome, *, notice_id: UUID | None = None
    ) -> PersonalBurstInput:
        """Persist a terminal rejection for a reserved receipt. The rejected body is never retained."""
        db = require_transaction("rejection")
        row = await db.fetchone(
            f"INSERT INTO {self.table} (id, config_id, provider, event_key, outcome, notice_id) "
            "VALUES (%(id)s, %(config_id)s, %(provider)s, %(event_key)s, %(outcome)s, %(notice)s) "
            f"RETURNING {self._select_list()}",
            {"id": uuid4(), **_key(receipt), "outcome": outcome, "notice": notice_id},
        )
        if row is None:
            raise RuntimeError("INSERT into personal_burst_inputs returned no row")
        return self.record_factory(row)

    async def link_notice(self, receipt: ReceiptKey, notice_id: UUID) -> bool:
        """Attach the covering notice to an unlinked rejection; ``False`` when already linked or not a rejection."""
        db = require_transaction("notice link")
        row = await db.fetchone(
            f"UPDATE {self.table} SET notice_id = %(notice)s WHERE {_BY_RECEIPT} "
            "AND outcome <> 'accepted' AND notice_id IS NULL RETURNING id",
            {**_key(receipt), "notice": notice_id},
        )
        return row is not None


personal_burst_input_dao = PersonalBurstInputDAO()
