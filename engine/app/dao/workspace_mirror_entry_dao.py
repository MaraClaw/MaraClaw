from collections.abc import Sequence
from typing import ClassVar, final
from uuid import UUID

from app.dao.base import BaseDAO
from app.records.workspace_mirror_entry import WorkspaceMirrorEntryRecord


@final
class WorkspaceMirrorEntryDAO(BaseDAO[WorkspaceMirrorEntryRecord]):
    """Manifest of the durable workspace mirror: one row per agent and path, upserted once that path has reconciled."""

    table: ClassVar[str] = "agent_workspace_mirror_entries"
    pk: ClassVar[str] = "agent_id"
    columns: ClassVar[tuple[str, ...]] = (
        "agent_id",
        "path",
        "generation",
        "local_ino",
        "local_size",
        "local_mtime_ns",
        "local_ctime_ns",
        "local_mode",
        "content_sha256",
        "remote_etag",
        "remote_size",
        "remote_token",
        "skip_reason",
        "synced_at",
    )
    record_factory = staticmethod(WorkspaceMirrorEntryRecord.from_row)

    async def list_for_agent(self, agent_id: UUID) -> dict[str, WorkspaceMirrorEntryRecord]:
        """Every manifest row of one agent, keyed by workspace-relative path."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} WHERE agent_id = %(agent_id)s ORDER BY path",
                {"agent_id": agent_id},
            )
            records = [self._from_row(row) for row in rows]
            return {record.path: record for record in records}

    async def upsert(self, record: WorkspaceMirrorEntryRecord) -> None:
        """Insert or replace the row of ``(agent_id, path)``; ``synced_at`` becomes the write time."""
        async with self.session() as db:
            await db.execute(
                f"""
                INSERT INTO {self.table}
                    (agent_id, path, generation, local_ino, local_size, local_mtime_ns, local_ctime_ns, local_mode,
                     content_sha256, remote_etag, remote_size, remote_token, skip_reason)
                VALUES (
                    %(agent_id)s, %(path)s, %(generation)s, %(local_ino)s, %(local_size)s, %(local_mtime_ns)s,
                    %(local_ctime_ns)s, %(local_mode)s, %(content_sha256)s, %(remote_etag)s, %(remote_size)s,
                    %(remote_token)s, %(skip_reason)s
                )
                ON CONFLICT (agent_id, path) DO UPDATE
                SET generation = EXCLUDED.generation,
                    local_ino = EXCLUDED.local_ino,
                    local_size = EXCLUDED.local_size,
                    local_mtime_ns = EXCLUDED.local_mtime_ns,
                    local_ctime_ns = EXCLUDED.local_ctime_ns,
                    local_mode = EXCLUDED.local_mode,
                    content_sha256 = EXCLUDED.content_sha256,
                    remote_etag = EXCLUDED.remote_etag,
                    remote_size = EXCLUDED.remote_size,
                    remote_token = EXCLUDED.remote_token,
                    skip_reason = EXCLUDED.skip_reason,
                    synced_at = now()
                """,
                {
                    "agent_id": record.agent_id,
                    "path": record.path,
                    "generation": record.generation,
                    "local_ino": record.local_ino,
                    "local_size": record.local_size,
                    "local_mtime_ns": record.local_mtime_ns,
                    "local_ctime_ns": record.local_ctime_ns,
                    "local_mode": record.local_mode,
                    "content_sha256": record.content_sha256,
                    "remote_etag": record.remote_etag,
                    "remote_size": record.remote_size,
                    "remote_token": record.remote_token,
                    "skip_reason": record.skip_reason,
                },
            )

    async def delete_paths(self, agent_id: UUID, paths: Sequence[str]) -> int:
        """Drop the rows of ``paths`` for one agent and return how many existed."""
        if not paths:
            return 0
        async with self.session() as db:
            rows = await db.fetchall(
                f"DELETE FROM {self.table} WHERE agent_id = %(agent_id)s AND path = ANY(%(paths)s) RETURNING path",
                {"agent_id": agent_id, "paths": list(paths)},
            )
            return len(rows)


workspace_mirror_entry_dao = WorkspaceMirrorEntryDAO()
