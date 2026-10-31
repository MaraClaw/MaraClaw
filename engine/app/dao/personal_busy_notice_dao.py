"""Cooldown metadata for the static "not queued" notices (the notice row itself is a completed gateway row)."""

from typing import ClassVar, final
from uuid import UUID, uuid4

from app.dao.base import BaseDAO
from app.dao.personal_burst_sql import (
    SCOPE_COLUMNS,
    columns_of,
    require_parent_lock,
    require_transaction,
    scope_matches,
)
from app.records.personal_burst import PersonalBurstScope
from app.records.personal_burst_errors import PersonalBurstAnchorError
from app.records.personal_burst_notice import BUSY_NOTICE_ROUTING_REASON, BusyNoticeReason, PersonalBusyNotice

_COLUMNS = ("id", "gateway_id", *SCOPE_COLUMNS, "reason", "created_at")
_SCOPE_PARAMS = ", ".join(f"%({column})s" for column in SCOPE_COLUMNS)


@final
class PersonalBusyNoticeDAO(BaseDAO[PersonalBusyNotice]):
    table: ClassVar[str] = "personal_busy_notices"
    columns: ClassVar[tuple[str, ...]] = _COLUMNS
    record_factory = staticmethod(PersonalBusyNotice.from_row)

    async def find_recent(self, scope: PersonalBurstScope, cooldown_seconds: float) -> PersonalBusyNotice | None:
        """Latest notice for the FULL frozen scope still inside the DB-time cooldown."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {columns_of('n', _COLUMNS)} FROM {self.table} n WHERE {scope_matches('n')} "
                "AND n.created_at > clock_timestamp() - make_interval(secs => %(cooldown)s::float8) "
                "ORDER BY n.created_at DESC LIMIT 1",
                {**scope.params(), "cooldown": cooldown_seconds},
            )
        return self.record_factory(row) if row is not None else None

    async def create_notice(
        self, scope: PersonalBurstScope, gateway_id: UUID, reason: BusyNoticeReason
    ) -> PersonalBusyNotice:
        """Record cooldown metadata for a static source row (completed, no conversation/senders/content)."""
        db = require_transaction("notice create")
        await require_parent_lock(db, scope.agent_id, scope.shared_session_id)
        row = await db.fetchone(
            f"INSERT INTO {self.table} (id, gateway_id, {', '.join(SCOPE_COLUMNS)}, reason) "
            f"SELECT %(id)s, g.id, {_SCOPE_PARAMS}, %(reason)s FROM gateway_messages g "
            "WHERE g.id = %(gateway_id)s AND g.agent_id = %(agent_id)s AND g.status = 'completed' "
            "AND g.conversation_id IS NULL AND g.sender_agent_id IS NULL AND g.sender_user_id IS NULL "
            "AND g.content = '' AND g.routing_reason = %(routing)s AND COALESCE(g.result, '') <> '' "
            f"RETURNING {self._select_list()}",
            {
                **scope.params(),
                "id": uuid4(),
                "gateway_id": gateway_id,
                "reason": reason,
                "routing": BUSY_NOTICE_ROUTING_REASON,
            },
        )
        if row is None:
            raise PersonalBurstAnchorError(gateway_id)
        return self.record_factory(row)


personal_busy_notice_dao = PersonalBusyNoticeDAO()
