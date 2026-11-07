"""Durable history of committed broadcasts; rows commit or roll back with the fan-out they describe."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.broadcast_history import BroadcastHistoryRecord, NewBroadcastHistory


class BroadcastHistoryDAO(BaseDAO[BroadcastHistoryRecord]):
    table: ClassVar[str] = "broadcast_history"
    pk: ClassVar[str] = "broadcast_id"
    columns: ClassVar[tuple[str, ...]] = (
        "broadcast_id",
        "tenant_id",
        "sender_id",
        "sender_name",
        "title",
        "body",
        "send_email",
        "users_notified",
        "agents_notified",
        "emails_sent",
        "published_at",
    )
    record_factory: Callable[[Mapping[str, object]], BroadcastHistoryRecord] = BroadcastHistoryRecord.from_row

    async def publish(self, entry: NewBroadcastHistory) -> None:
        """Insert inside the caller's transaction; ``published_at`` comes from the database clock."""
        async with self.session() as db:
            await db.execute(
                """INSERT INTO broadcast_history
                   (broadcast_id, tenant_id, sender_id, sender_name, title, body, send_email,
                    users_notified, agents_notified, emails_sent)
                   VALUES (%(broadcast_id)s, %(tenant_id)s, %(sender_id)s, %(sender_name)s, %(title)s, %(body)s,
                           %(send_email)s, %(users_notified)s, %(agents_notified)s, %(emails_sent)s)""",
                asdict(entry),
            )

    async def list_page(
        self, tenant_id: UUID, *, limit: int, before: tuple[datetime, UUID] | None = None
    ) -> Sequence[BroadcastHistoryRecord]:
        """Latest-first keyset page of one tenant plus one lookahead row."""
        params: dict[str, UUID | int | datetime] = {"tenant_id": tenant_id, "limit": limit + 1}
        seek = ""
        if before is not None:
            seek = "AND (published_at, broadcast_id) < (%(before_at)s, %(before_id)s)"
            params["before_at"], params["before_id"] = before
        async with self.session() as db:
            rows = await db.fetchall(
                f"""SELECT {self._select_list()} FROM broadcast_history
                    WHERE tenant_id = %(tenant_id)s {seek}
                    ORDER BY published_at DESC, broadcast_id DESC LIMIT %(limit)s""",
                params,
            )
        return [self.record_factory(row) for row in rows]


broadcast_history_dao = BroadcastHistoryDAO()
