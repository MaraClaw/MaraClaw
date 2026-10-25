"""Persistence for global OpenClaw upgrade notices and per-agent upgrade runs."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, final
from uuid import UUID, uuid4

from app.core.json_types import int_from_row
from app.core.notification_kinds import NotificationKind
from app.core.row_memo import memo_drop
from app.dao.base import BaseDAO
from app.db.types import as_jsonb
from app.records.openclaw_update import OpenClawUpdateNoticeRecord, OpenClawUpdateRunRecord, RunJournal

_NOTICE_COLUMNS = ("id", "request_id", "title", "body", "mandatory", "recipients", "created_by", "created_at")
_RUN_COLUMNS = ("id", "notice_id", "agent_id", "requested_by", "state", "failure_code", "journal", "created_at")
SUCCEEDED_SWEEP_LIMIT = 100

# A notice is outstanding for an agent when it is newer than the agent and no
# successful upgrade for the same or a newer notice exists.
_OUTSTANDING = (
    "FROM openclaw_update_notices n JOIN agents a ON a.id = %(agent_id)s "
    "WHERE n.created_at > a.created_at AND NOT EXISTS ("
    "SELECT 1 FROM openclaw_update_runs r JOIN openclaw_update_notices done ON done.id = r.notice_id "
    "WHERE r.agent_id = a.id AND r.state = 'succeeded' "
    "AND (done.created_at, done.id) >= (n.created_at, n.id))"
)
_FANOUT = (
    "WITH inserted AS ("
    "INSERT INTO notifications (id, user_id, type, title, body, link, ref_id, sender_name, is_read) "
    "SELECT gen_random_uuid(), u.id, %(notification_kind)s, %(title)s, %(body)s, %(link)s, %(notice_id)s, "
    "%(sender)s, FALSE FROM users u "
    "JOIN identities i ON i.id = u.identity_id JOIN tenants t ON t.id = u.tenant_id "
    "WHERE u.is_active IS TRUE AND u.role <> 'platform_admin' "
    "AND i.is_active IS TRUE AND i.is_platform_admin IS FALSE AND t.is_active IS TRUE "
    "RETURNING 1) SELECT count(*) AS recipients FROM inserted"
)
# Publication keeps its own agent-visible CAS: the agent must still be desired-running.
_PUBLISH_AGENT = (
    "UPDATE agents SET container_id = %(new)s, container_port = %(port)s, openclaw_image_id = %(image)s, "
    "status = 'running', last_active_at = clock_timestamp(), updated_at = clock_timestamp() "
    "WHERE id = %(agent_id)s AND container_id IS NOT DISTINCT FROM %(old)s "
    "AND status IN ('running', 'idle') AND is_expired IS FALSE AND setup_pending IS FALSE "
    "AND (expires_at IS NULL OR expires_at > clock_timestamp()) "
    "AND (tenant_id IS NULL OR EXISTS (SELECT 1 FROM tenants t WHERE t.id = agents.tenant_id AND t.is_active IS TRUE)) "
    "AND EXISTS (SELECT 1 FROM users u WHERE u.id = agents.creator_id AND u.is_active IS TRUE) "
    "RETURNING id"
)


class _PublishLostError(Exception):
    """The agent changed under the cutover; abort the publication transaction."""


@final
class OpenClawUpdateNoticeDAO(BaseDAO[OpenClawUpdateNoticeRecord]):
    table: ClassVar[str] = "openclaw_update_notices"
    columns: ClassVar[tuple[str, ...]] = _NOTICE_COLUMNS
    record_factory = staticmethod(OpenClawUpdateNoticeRecord.from_row)

    async def publish(
        self,
        *,
        request_id: UUID,
        title: str,
        body: str,
        mandatory: bool,
        actor_id: UUID | None,
        sender_name: str,
    ) -> tuple[OpenClawUpdateNoticeRecord, bool]:
        """Insert the notice and fan out to every eligible member in one transaction.

        Returns ``(notice, created)``; a replayed ``request_id`` returns the stored notice.
        """
        notice_id = uuid4()
        async with self.session() as db:
            row = await db.fetchone(
                "INSERT INTO openclaw_update_notices (id, request_id, title, body, mandatory, created_by) "
                "VALUES (%(id)s, %(request_id)s, %(title)s, %(body)s, %(mandatory)s, %(actor)s) "
                f"ON CONFLICT (request_id) DO NOTHING RETURNING {self._select_list()}",
                {
                    "id": notice_id,
                    "request_id": request_id,
                    "title": title,
                    "body": body,
                    "mandatory": mandatory,
                    "actor": actor_id,
                },
            )
            if row is None:
                existing = await db.fetchone(
                    f"SELECT {self._select_list()} FROM openclaw_update_notices WHERE request_id = %(request_id)s",
                    {"request_id": request_id},
                )
                if existing is None:
                    raise RuntimeError("notice vanished after request_id conflict")
                return OpenClawUpdateNoticeRecord.from_row(existing), False
            recipients = int_from_row(
                await db.fetchval(
                    _FANOUT,
                    {
                        "notification_kind": NotificationKind.OPENCLAW_UPDATE.value,
                        "title": title,
                        "body": body,
                        "link": f"/app/updates/{notice_id}",
                        "notice_id": notice_id,
                        "sender": sender_name,
                    },
                )
            )
            stored = await db.fetchone(
                "UPDATE openclaw_update_notices SET recipients = %(n)s WHERE id = %(id)s "
                f"RETURNING {self._select_list()}",
                {"n": recipients, "id": notice_id},
            )
            if stored is None:
                raise RuntimeError("notice vanished during fan-out")
            return OpenClawUpdateNoticeRecord.from_row(stored), True

    async def selected_for_agent(self, agent_id: UUID) -> OpenClawUpdateNoticeRecord | None:
        """Newest outstanding mandatory notice, else the newest outstanding optional one."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list('n')} {_OUTSTANDING} "
                "ORDER BY n.mandatory DESC, n.created_at DESC, n.id DESC LIMIT 1",
                {"agent_id": agent_id},
            )
            return OpenClawUpdateNoticeRecord.from_row(row) if row is not None else None

    async def gate_state(self, agent_id: UUID) -> tuple[bool, UUID | None]:
        """``(active run exists, newest outstanding mandatory notice id)`` for one agent."""
        async with self.session() as db:
            active = await db.fetchval(
                "SELECT 1 FROM openclaw_update_runs WHERE agent_id = %(agent_id)s AND state IN ('pending', 'cutover')",
                {"agent_id": agent_id},
            )
            required = await db.fetchval(
                f"SELECT n.id {_OUTSTANDING} AND n.mandatory IS TRUE ORDER BY n.created_at DESC, n.id DESC LIMIT 1",
                {"agent_id": agent_id},
            )
            return active is not None, UUID(str(required)) if required is not None else None


@final
class OpenClawUpdateRunDAO(BaseDAO[OpenClawUpdateRunRecord]):
    table: ClassVar[str] = "openclaw_update_runs"
    columns: ClassVar[tuple[str, ...]] = _RUN_COLUMNS
    record_factory = staticmethod(OpenClawUpdateRunRecord.from_row)

    async def create_pending(
        self, *, notice_id: UUID, agent_id: UUID, requested_by: UUID | None
    ) -> OpenClawUpdateRunRecord | None:
        """Insert a pending run; ``None`` when the agent already has an active one."""
        async with self.session() as db:
            row = await db.fetchone(
                "INSERT INTO openclaw_update_runs (id, notice_id, agent_id, requested_by, state) "
                "VALUES (%(id)s, %(notice_id)s, %(agent_id)s, %(requested_by)s, 'pending') "
                "ON CONFLICT (agent_id) WHERE state IN ('pending', 'cutover') DO NOTHING "
                f"RETURNING {self._select_list()}",
                {"id": uuid4(), "notice_id": notice_id, "agent_id": agent_id, "requested_by": requested_by},
            )
            return OpenClawUpdateRunRecord.from_row(row) if row is not None else None

    async def active_for_agent(self, agent_id: UUID) -> OpenClawUpdateRunRecord | None:
        return await self._one(
            "WHERE agent_id = %(agent_id)s AND state IN ('pending', 'cutover')", {"agent_id": agent_id}
        )

    async def succeeded_for(self, agent_id: UUID, notice_id: UUID) -> OpenClawUpdateRunRecord | None:
        return await self._one(
            "WHERE agent_id = %(agent_id)s AND notice_id = %(notice_id)s AND state = 'succeeded'",
            {"agent_id": agent_id, "notice_id": notice_id},
        )

    async def latest_for_notice(self, agent_id: UUID, notice_id: UUID) -> OpenClawUpdateRunRecord | None:
        return await self._one(
            "WHERE agent_id = %(agent_id)s AND notice_id = %(notice_id)s ORDER BY created_at DESC LIMIT 1",
            {"agent_id": agent_id, "notice_id": notice_id},
        )

    async def list_active(self) -> Sequence[OpenClawUpdateRunRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM openclaw_update_runs "
                "WHERE state IN ('pending', 'cutover') ORDER BY created_at, id"
            )
            return [OpenClawUpdateRunRecord.from_row(row) for row in rows]

    async def list_recent_succeeded(self, since: datetime, after: UUID | None) -> Sequence[OpenClawUpdateRunRecord]:
        """Bounded keyset page by completion time, not the run's creation time."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM openclaw_update_runs "
                "WHERE state = 'succeeded' AND updated_at >= %(since)s "
                "AND (%(after)s::uuid IS NULL OR id > %(after)s) ORDER BY id LIMIT %(limit)s",
                {"since": since, "after": after, "limit": SUCCEEDED_SWEEP_LIMIT},
            )
            return [OpenClawUpdateRunRecord.from_row(row) for row in rows]

    async def begin_cutover(self, run_id: UUID, journal: RunJournal) -> bool:
        """CAS ``pending`` -> ``cutover`` with the journal written first."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE openclaw_update_runs SET state = 'cutover', journal = %(journal)s, "
                "updated_at = clock_timestamp() WHERE id = %(id)s AND state = 'pending' RETURNING id",
                {"id": run_id, "journal": as_jsonb(journal)},
            )
            return row is not None

    async def set_journal(self, run_id: UUID, journal: RunJournal) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE openclaw_update_runs SET journal = %(journal)s, updated_at = clock_timestamp() "
                "WHERE id = %(id)s AND state = 'cutover'",
                {"id": run_id, "journal": as_jsonb(journal)},
            )

    async def finish_failed(self, run_id: UUID, code: str) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE openclaw_update_runs SET state = 'failed', failure_code = %(code)s, "
                "updated_at = clock_timestamp() WHERE id = %(id)s AND state IN ('pending', 'cutover')",
                {"id": run_id, "code": code},
            )

    async def fail_pending(self, run_id: UUID, code: str) -> bool:
        """Terminate a run that has not entered cutover; never touches one a worker already owns."""
        async with self.session() as db:
            row = await db.fetchone(
                "UPDATE openclaw_update_runs SET state = 'failed', failure_code = %(code)s, "
                "updated_at = clock_timestamp() WHERE id = %(id)s AND state = 'pending' RETURNING id",
                {"id": run_id, "code": code},
            )
            return row is not None

    async def publish_success(
        self,
        run_id: UUID,
        agent_id: UUID,
        *,
        old_container_id: str,
        new_container_id: str,
        port: int | None,
        image_id: str,
    ) -> bool:
        """Atomically repoint the agent and mark the run succeeded, or change nothing."""
        try:
            async with self.session() as db:
                agent_row = await db.fetchone(
                    _PUBLISH_AGENT,
                    {
                        "agent_id": agent_id,
                        "old": old_container_id,
                        "new": new_container_id,
                        "port": port,
                        "image": image_id,
                    },
                )
                if agent_row is None:
                    raise _PublishLostError
                done = await db.fetchone(
                    "UPDATE openclaw_update_runs SET state = 'succeeded', updated_at = clock_timestamp() "
                    "WHERE id = %(id)s AND state = 'cutover' RETURNING id",
                    {"id": run_id},
                )
                if done is None:
                    raise _PublishLostError
        except _PublishLostError:
            return False
        finally:
            memo_drop("agent", agent_id)
        return True

    async def pin_image(self, agent_id: UUID, image_id: str) -> None:
        """Make ordinary starts use ``image_id``."""
        async with self.session() as db:
            await db.execute(
                "UPDATE agents SET openclaw_image_id = %(image)s, updated_at = clock_timestamp() WHERE id = %(id)s",
                {"id": agent_id, "image": image_id},
            )
        memo_drop("agent", agent_id)

    async def mark_agent_error(self, agent_id: UUID) -> None:
        async with self.session() as db:
            await db.execute(
                "UPDATE agents SET status = 'error', updated_at = clock_timestamp() WHERE id = %(id)s",
                {"id": agent_id},
            )
        memo_drop("agent", agent_id)

    async def _one(self, tail: str, params: dict[str, object]) -> OpenClawUpdateRunRecord | None:
        async with self.session() as db:
            row = await db.fetchone(f"SELECT {self._select_list()} FROM openclaw_update_runs {tail}", params)
            return OpenClawUpdateRunRecord.from_row(row) if row is not None else None


openclaw_update_notice_dao = OpenClawUpdateNoticeDAO()
openclaw_update_run_dao = OpenClawUpdateRunDAO()
