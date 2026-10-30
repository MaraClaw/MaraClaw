"""DAO for notifications"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, Literal, assert_never, final
from uuid import UUID

from app.core.json_types import int_from_row, uuid_from_row
from app.core.notification_kinds import NotificationKind
from app.dao.base import BaseDAO
from app.dao.company_transfer_disposition_sql import VISIBLE_NOTIFICATION_SQL
from app.records.notification import NotificationRecord

_NOTIFICATION_COLUMNS = (
    "id",
    "user_id",
    "agent_id",
    "type",
    "title",
    "body",
    "link",
    "ref_id",
    "sender_name",
    "is_read",
    "archived_at",
    "created_at",
)


def _user_list_clauses(
    view: Literal["active", "archived"], unread_only: bool, types: Sequence[NotificationKind] | None
) -> list[str]:
    clauses = ["user_id = %(user_id)s", VISIBLE_NOTIFICATION_SQL]
    match view:
        case "active":
            clauses.append("archived_at IS NULL")
        case "archived":
            clauses.append("archived_at IS NOT NULL")
        case unreachable:
            assert_never(unreachable)
    if unread_only:
        clauses.append("is_read IS FALSE")
    if types is not None:
        clauses.append("type = ANY(%(types)s)")
    return clauses


@final
class NotificationDAO(BaseDAO[NotificationRecord]):
    """DAO for notification rows."""

    table: ClassVar[str] = "notifications"
    columns: ClassVar[tuple[str, ...]] = _NOTIFICATION_COLUMNS
    record_factory = staticmethod(NotificationRecord.from_row)

    async def list_unread_for_agent(self, agent_id: UUID, *, limit: int = 10) -> Sequence[NotificationRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM notifications "
                "WHERE agent_id = %(agent_id)s AND is_read IS FALSE "
                "ORDER BY created_at ASC LIMIT %(limit)s",
                {"agent_id": agent_id, "limit": limit},
            )
            return [NotificationRecord.from_row(row) for row in rows]

    async def list_for_user(
        self,
        user_id: UUID,
        *,
        view: Literal["active", "archived"] = "active",
        unread_only: bool = False,
        types: Sequence[NotificationKind] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[NotificationRecord]:
        params: dict[str, object] = {"user_id": user_id, "limit": limit, "offset": offset}
        clauses = _user_list_clauses(view, unread_only, types)
        if types is not None:
            params["types"] = [kind.value for kind in types]
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM notifications "
                f"WHERE {where} ORDER BY created_at DESC, id DESC "
                "OFFSET %(offset)s LIMIT %(limit)s",
                params,
            )
            return [NotificationRecord.from_row(row) for row in rows]

    async def list_page_for_user(
        self,
        user_id: UUID,
        *,
        view: Literal["active", "archived"] = "active",
        unread_only: bool = False,
        types: Sequence[NotificationKind] | None = None,
        limit: int = 50,
        before: tuple[datetime, UUID] | None = None,
    ) -> Sequence[NotificationRecord]:
        """Return a live seek page plus one lookahead row, under the current user's filters."""
        params: dict[str, UUID | int | datetime | list[str]] = {"user_id": user_id, "limit": limit + 1}
        clauses = _user_list_clauses(view, unread_only, types)
        if types is not None:
            params["types"] = [kind.value for kind in types]
        if before is not None:
            clauses.append("(created_at, id) < (%(before_created_at)s, %(before_id)s)")
            params["before_created_at"], params["before_id"] = before
        where = " AND ".join(clauses)
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM notifications "
                f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT %(limit)s",
                params,
            )
            return [NotificationRecord.from_row(row) for row in rows]

    async def count_unread_for_user(
        self,
        user_id: UUID,
        *,
        types: Sequence[NotificationKind] | None = None,
    ) -> int:
        params: dict[str, object] = {"user_id": user_id}
        clauses = ["user_id = %(user_id)s", "is_read IS FALSE", "archived_at IS NULL", VISIBLE_NOTIFICATION_SQL]
        if types is not None:
            clauses.append("type = ANY(%(types)s)")
            params["types"] = [kind.value for kind in types]
        where = " AND ".join(clauses)
        async with self.session() as db:
            value = await db.fetchval(
                f"SELECT COUNT(*) FROM notifications WHERE {where}",
                params,
            )
            return int_from_row(value)

    async def mark_read(self, ids: Sequence[UUID]) -> None:
        if not ids:
            return
        async with self.session() as db:
            await db.execute(
                "UPDATE notifications SET is_read = TRUE WHERE id = ANY(%(ids)s)",
                {"ids": list(ids)},
            )

    async def mark_read_for_user(self, notification_id: UUID, user_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE notifications SET is_read = TRUE WHERE id = %(id)s AND user_id = %(user_id)s AND "
                + VISIBLE_NOTIFICATION_SQL,
                {"id": notification_id, "user_id": user_id},
            )

    async def mark_selected_read_for_user(self, ids: Sequence[UUID], user_id: UUID) -> bool:
        distinct_ids = sorted(set(ids))
        params = {"ids": distinct_ids, "user_id": user_id}
        where = "id = ANY(%(ids)s) AND user_id = %(user_id)s AND " + VISIBLE_NOTIFICATION_SQL
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT id FROM notifications WHERE {where} ORDER BY id FOR UPDATE",
                params,
            )
            if {uuid_from_row(row["id"]) for row in rows} != set(distinct_ids):
                return False
            await db.execute(f"UPDATE notifications SET is_read = TRUE WHERE {where}", params)
            return True

    async def _set_selected_archive_for_user(self, ids: Sequence[UUID], user_id: UUID, *, archive: bool) -> bool:
        distinct_ids = sorted(set(ids))
        params = {"ids": distinct_ids, "user_id": user_id}
        where = "id = ANY(%(ids)s) AND user_id = %(user_id)s AND " + VISIBLE_NOTIFICATION_SQL
        async with self.session() as db:
            rows = await db.fetchall(f"SELECT id FROM notifications WHERE {where} ORDER BY id FOR UPDATE", params)
            if {uuid_from_row(row["id"]) for row in rows} != set(distinct_ids):
                return False
            assignment = "COALESCE(archived_at, now())" if archive else "NULL"
            await db.execute(f"UPDATE notifications SET archived_at = {assignment} WHERE id = ANY(%(ids)s)", params)
            return True

    async def archive_selected_for_user(self, ids: Sequence[UUID], user_id: UUID) -> bool:
        return await self._set_selected_archive_for_user(ids, user_id, archive=True)

    async def restore_selected_for_user(self, ids: Sequence[UUID], user_id: UUID) -> bool:
        return await self._set_selected_archive_for_user(ids, user_id, archive=False)

    async def mark_all_read_for_user(self, user_id: UUID, *, types: Sequence[NotificationKind] | None = None) -> None:
        params: dict[str, object] = {"user_id": user_id}
        clauses = ["user_id = %(user_id)s", "is_read IS FALSE", "archived_at IS NULL", VISIBLE_NOTIFICATION_SQL]
        if types is not None:
            clauses.append("type = ANY(%(types)s)")
            params["types"] = [kind.value for kind in types]
        where = " AND ".join(clauses)
        async with self.session() as db:
            await db.execute(
                f"UPDATE notifications SET is_read = TRUE WHERE {where}",
                params,
            )

    async def archive_for_user(self, notification_id: UUID, user_id: UUID) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE notifications SET archived_at = COALESCE(archived_at, now()) "
                "WHERE id = %(id)s AND user_id = %(user_id)s AND " + VISIBLE_NOTIFICATION_SQL + " RETURNING id",
                {"id": notification_id, "user_id": user_id},
            )
            return row is not None

    async def restore_for_user(self, notification_id: UUID, user_id: UUID) -> bool:
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE notifications SET archived_at = NULL WHERE id = %(id)s AND user_id = %(user_id)s AND "
                + VISIBLE_NOTIFICATION_SQL
                + " RETURNING id",
                {"id": notification_id, "user_id": user_id},
            )
            return row is not None

    async def delete_system_task_failed(
        self,
        *,
        user_id: UUID,
        agent_id: UUID,
    ) -> None:
        """Clear oneshot failure notifications for a user/agent pair."""
        async with self.session() as db:
            await db.execute(
                "DELETE FROM notifications "
                "WHERE user_id = %(user_id)s AND ref_id = %(agent_id)s "
                "AND type = %(notification_kind)s AND title LIKE %(title_pat)s",
                {
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "notification_kind": NotificationKind.SYSTEM.value,
                    "title_pat": "%task failed%",
                },
            )

    async def latest_system_task_failed(self, *, user_id: UUID, ref_id: UUID) -> NotificationRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM notifications "
                "WHERE user_id = %(user_id)s AND ref_id = %(ref_id)s "
                "AND type = %(notification_kind)s AND title LIKE %(title_pat)s "
                "AND " + VISIBLE_NOTIFICATION_SQL + " " + "ORDER BY created_at DESC LIMIT 1",
                {
                    "user_id": user_id,
                    "ref_id": ref_id,
                    "notification_kind": NotificationKind.SYSTEM.value,
                    "title_pat": "%task failed%",
                },
            )
            return NotificationRecord.from_row(row) if row is not None else None


notification_dao = NotificationDAO()
