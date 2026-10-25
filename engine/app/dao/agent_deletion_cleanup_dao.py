from pathlib import Path
from uuid import UUID

from app.core.json_types import uuid_from_row
from app.db.session import connection_ctx, get_connection
from app.records.agent_deletion_cleanup import AgentDeletionCleanupRecord


class AgentDeletionCleanupDAO:
    async def enqueue(self, agent_id: UUID, workspace: Path, archive: Path) -> AgentDeletionCleanupRecord:
        db = get_connection()
        if db is None:
            raise RuntimeError("Deleted-agent cleanup must join the owning deletion transaction")
        row = await db.fetchone(
            """
            INSERT INTO agent_deletion_cleanup
                (agent_id, name, creator_id, tenant_id, container_id, workspace_path, archive_path, task_history)
            SELECT a.id, a.name, a.creator_id, a.tenant_id, a.container_id, %(workspace)s, %(archive)s,
                jsonb_build_object('agent_id', a.id, 'archived_at', statement_timestamp(), 'tasks',
                    COALESCE((SELECT jsonb_agg(
                        (to_jsonb(t) - 'agent_id') || jsonb_build_object('logs',
                            COALESCE((SELECT jsonb_agg(to_jsonb(l) - 'task_id' ORDER BY l.created_at, l.id)
                                      FROM task_logs l WHERE l.task_id=t.id), '[]'::jsonb))
                        ORDER BY t.created_at, t.id) FROM tasks t WHERE t.agent_id=a.id), '[]'::jsonb))::text
            FROM agents a WHERE a.id=%(id)s
            ON CONFLICT (agent_id) DO UPDATE SET agent_id=EXCLUDED.agent_id
            RETURNING *
            """,
            {"id": agent_id, "workspace": str(workspace), "archive": str(archive)},
        )
        if row is None:
            raise RuntimeError("Cannot freeze cleanup for a missing agent")
        return AgentDeletionCleanupRecord.from_row(row)

    async def pending(self) -> list[UUID]:
        async with connection_ctx() as db:
            rows = await db.fetchall(
                "SELECT agent_id FROM agent_deletion_cleanup WHERE phase <> 'completed' "
                "ORDER BY attempts, created_at, agent_id LIMIT 100"
            )
        return [uuid_from_row(row["agent_id"]) for row in rows]

    async def get_pending(self, agent_id: UUID) -> AgentDeletionCleanupRecord | None:
        async with connection_ctx() as db:
            row = await db.fetchone(
                "SELECT * FROM agent_deletion_cleanup c WHERE c.agent_id=%(id)s AND c.phase <> 'completed' "
                "AND NOT EXISTS (SELECT 1 FROM agents a WHERE a.id=c.agent_id)",
                {"id": agent_id},
            )
        return AgentDeletionCleanupRecord.from_row(row) if row is not None else None

    async def begin_attempt(self, agent_id: UUID) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE agent_deletion_cleanup SET attempts=attempts+1, last_error=NULL, phase='removing' "
                "WHERE agent_id=%(id)s AND phase <> 'completed'",
                {"id": agent_id},
            )

    async def progress(self, agent_id: UUID, *, completed: bool = False, error: str | None = None) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE agent_deletion_cleanup SET last_error=%(error)s, "
                "phase=CASE WHEN %(completed)s THEN 'completed' WHEN %(error)s::text IS NULL THEN 'archiving' "
                "ELSE phase END, completed_at=CASE WHEN %(completed)s THEN now() ELSE NULL END "
                "WHERE agent_id=%(id)s AND phase <> 'completed'",
                {"id": agent_id, "completed": completed, "error": error},
            )


agent_deletion_cleanup_dao = AgentDeletionCleanupDAO()
