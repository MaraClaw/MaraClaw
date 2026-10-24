from typing import final
from uuid import UUID

from app.dao.base import BaseDAO
from app.db.session import connection_ctx, optional_connection_ctx
from app.records.company_transfer_cleanup import TransferCleanupRecord


@final
class CompanyTransferCleanupDAO(BaseDAO[TransferCleanupRecord]):
    table = "company_transfer_runtime_cleanup"
    pk = "agent_id"
    columns = ("agent_id", "transfer_request_id", "choice")
    record_factory = staticmethod(TransferCleanupRecord.from_row)

    async def pending(self) -> list[TransferCleanupRecord]:
        async with self.session() as db:
            rows = await db.fetchall(
                "SELECT agent_id,transfer_request_id,choice FROM company_transfer_runtime_cleanup "
                "WHERE state='pending' ORDER BY created_at,agent_id LIMIT 100"
            )
        return [TransferCleanupRecord.from_row(row) for row in rows]

    async def is_pending(self, agent_id: UUID) -> bool:
        async with optional_connection_ctx() as db:
            if db is None:
                return False
            return (
                await db.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM company_transfer_runtime_cleanup "
                    "WHERE agent_id=%(id)s AND state='pending')",
                    {"id": agent_id},
                )
                is True
            )

    async def record_attempt(self, agent_id: UUID, error: str | None) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "UPDATE company_transfer_runtime_cleanup SET attempts=attempts+1,last_error=%(error)s, "
                "state=CASE WHEN %(error)s IS NULL THEN 'completed' ELSE 'pending' END, "
                "completed_at=CASE WHEN %(error)s IS NULL THEN now() ELSE NULL END "
                "WHERE agent_id=%(id)s AND state='pending'",
                {"id": agent_id, "error": error},
            )


company_transfer_cleanup_dao = CompanyTransferCleanupDAO()
