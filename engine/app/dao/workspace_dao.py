"""DAO for workspace_file_revisions and workspace_edit_locks (psycopg)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from app.core.json_types import int_from_row
from app.dao.base import BaseDAO
from app.records.workspace import WorkspaceEditLockRecord, WorkspaceFileRevisionRecord

_REVISION_COLUMNS = (
    "id",
    "agent_id",
    "path",
    "operation",
    "actor_type",
    "actor_id",
    "session_id",
    "before_content",
    "after_content",
    "content_hash",
    "group_key",
    "created_at",
    "updated_at",
)

_LOCK_COLUMNS = (
    "id",
    "agent_id",
    "path",
    "user_id",
    "session_id",
    "expires_at",
    "heartbeat_count",
    "created_at",
    "updated_at",
)


class WorkspaceFileRevisionDAO(BaseDAO[WorkspaceFileRevisionRecord]):
    table: ClassVar[str] = "workspace_file_revisions"
    columns: ClassVar[tuple[str, ...]] = _REVISION_COLUMNS
    record_factory = staticmethod(WorkspaceFileRevisionRecord.from_row)

    async def get_for_agent(self, revision_id: UUID, agent_id: UUID) -> WorkspaceFileRevisionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM workspace_file_revisions "
                + "WHERE id = %(id)s AND agent_id = %(agent_id)s",
                {"id": revision_id, "agent_id": agent_id},
            )
            return WorkspaceFileRevisionRecord.from_row(row) if row else None

    async def list_for_path(
        self,
        agent_id: UUID,
        path: str,
        *,
        limit: int = 50,
    ) -> Sequence[WorkspaceFileRevisionRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM workspace_file_revisions "
                + "WHERE agent_id = %(agent_id)s AND path = %(path)s "
                + "ORDER BY created_at DESC LIMIT %(limit)s",
                {"agent_id": agent_id, "path": path, "limit": limit},
            )
            return [WorkspaceFileRevisionRecord.from_row(row) for row in rows]

    async def find_mergeable_autosave(
        self,
        *,
        agent_id: UUID,
        path: str,
        actor_id: UUID,
        group_key: str,
        cutoff: datetime,
    ) -> WorkspaceFileRevisionRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM workspace_file_revisions "
                + "WHERE agent_id = %(agent_id)s AND path = %(path)s "
                + "AND actor_type = 'user' AND actor_id = %(actor_id)s "
                + "AND group_key = %(group_key)s AND operation = 'autosave' "
                + "AND updated_at >= %(cutoff)s "
                + "ORDER BY updated_at DESC LIMIT 1",
                {
                    "agent_id": agent_id,
                    "path": path,
                    "actor_id": actor_id,
                    "group_key": group_key,
                    "cutoff": cutoff,
                },
            )
            return WorkspaceFileRevisionRecord.from_row(row) if row else None


class WorkspaceEditLockDAO(BaseDAO[WorkspaceEditLockRecord]):
    table: ClassVar[str] = "workspace_edit_locks"
    columns: ClassVar[tuple[str, ...]] = _LOCK_COLUMNS
    record_factory = staticmethod(WorkspaceEditLockRecord.from_row)

    async def get_for_path(self, agent_id: UUID, path: str) -> WorkspaceEditLockRecord | None:
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM workspace_edit_locks "
                + "WHERE agent_id = %(agent_id)s AND path = %(path)s",
                {"agent_id": agent_id, "path": path},
            )
            return WorkspaceEditLockRecord.from_row(row) if row else None

    async def list_active(self, agent_id: UUID, *, now: datetime) -> Sequence[WorkspaceEditLockRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM workspace_edit_locks "
                + "WHERE agent_id = %(agent_id)s AND expires_at > %(now)s "
                + "ORDER BY path ASC",
                {"agent_id": agent_id, "now": now},
            )
            return [WorkspaceEditLockRecord.from_row(row) for row in rows]

    async def delete_expired(self, *, now: datetime) -> int:
        async with self.session() as db:
            value = await db.fetchval(
                "WITH deleted AS ("
                + "  DELETE FROM workspace_edit_locks WHERE expires_at <= %(now)s RETURNING 1"
                + ") SELECT COUNT(*) FROM deleted",
                {"now": now},
            )
            return int_from_row(value)

    async def delete_for_user_path(
        self,
        *,
        agent_id: UUID,
        path: str,
        user_id: UUID,
    ) -> None:
        async with self.session() as db:
            await db.execute(
                "DELETE FROM workspace_edit_locks "
                + "WHERE agent_id = %(agent_id)s AND path = %(path)s AND user_id = %(user_id)s",
                {"agent_id": agent_id, "path": path, "user_id": user_id},
            )

    async def upsert_for_path(
        self,
        *,
        agent_id: UUID,
        path: str,
        user_id: UUID,
        session_id: str | None,
        expires_at: datetime,
    ) -> WorkspaceEditLockRecord:
        """Acquire or refresh a lock for (agent_id, path)."""
        async with self.session() as db:
            row = await db.fetchone(
                "INSERT INTO workspace_edit_locks "
                + "(agent_id, path, user_id, session_id, expires_at, heartbeat_count) "
                + "VALUES (%(agent_id)s, %(path)s, %(user_id)s, %(session_id)s, %(expires_at)s, 1) "
                + "ON CONFLICT (agent_id, path) DO UPDATE SET "
                + "user_id = EXCLUDED.user_id, "
                + "session_id = EXCLUDED.session_id, "
                + "expires_at = EXCLUDED.expires_at, "
                + "heartbeat_count = workspace_edit_locks.heartbeat_count + 1, "
                + "updated_at = NOW() "
                + f"RETURNING {self._select_list()}",
                {
                    "agent_id": agent_id,
                    "path": path,
                    "user_id": user_id,
                    "session_id": session_id,
                    "expires_at": expires_at,
                },
            )
            if row is None:
                raise RuntimeError("upsert into workspace_edit_locks returned no row")
            return WorkspaceEditLockRecord.from_row(row)


workspace_file_revision_dao = WorkspaceFileRevisionDAO()
workspace_edit_lock_dao = WorkspaceEditLockDAO()
