"""Committed, database-clock retry reservations and operation-identity CAS."""

from typing import Final
from uuid import UUID

from app.db.connection import DbConnection
from app.records.agent_start import AgentStartOperation
from app.records.agent_start_retry import AgentStartRetryMetadata, StartFailureCode

START_RETRY_METADATA_DDL: Final = """
CREATE TABLE IF NOT EXISTS agent_start_retry_metadata (
    operation_id uuid PRIMARY KEY,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at timestamptz,
    next_retry_at timestamptz,
    last_failure_code text CHECK (last_failure_code IN (
        'capacity_exceeded', 'configuration_failed', 'readiness_failed', 'ownership_conflict',
        'runtime_failed', 'publication_failed', 'upgrade_in_progress'
    )),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_start_retry_due_idx
    ON agent_start_retry_metadata(next_retry_at) WHERE next_retry_at IS NOT NULL;
CREATE OR REPLACE FUNCTION sync_agent_start_retry_metadata() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM agent_start_retry_metadata WHERE operation_id = OLD.operation_id;
    ELSIF OLD.operation_id <> NEW.operation_id THEN
        DELETE FROM agent_start_retry_metadata WHERE operation_id = OLD.operation_id;
    ELSIF NEW.state <> 'pending' THEN
        UPDATE agent_start_retry_metadata SET next_retry_at = NULL, updated_at = now()
            WHERE operation_id = NEW.operation_id;
    END IF;
    RETURN NULL;
END;
$$;
CREATE OR REPLACE TRIGGER agent_start_retry_state_changed
    AFTER UPDATE OR DELETE ON agent_start_operations
    FOR EACH ROW EXECUTE FUNCTION sync_agent_start_retry_metadata();
"""


class AgentStartRetryDAO:
    async def get(self, db: DbConnection, operation_id: UUID) -> AgentStartRetryMetadata | None:
        row = await db.fetchone(
            "SELECT * FROM agent_start_retry_metadata WHERE operation_id=%(op)s", {"op": operation_id}
        )
        return AgentStartRetryMetadata.from_row(row) if row is not None else None

    async def initialize(self, db: DbConnection, operation: AgentStartOperation) -> None:
        await db.execute(
            "INSERT INTO agent_start_retry_metadata(operation_id,next_retry_at) VALUES (%(op)s,now()) "
            "ON CONFLICT (operation_id) DO NOTHING",
            {"op": operation.operation_id},
        )

    async def lock_pending(self, db: DbConnection, operation: AgentStartOperation) -> bool:
        return (
            await db.fetchone(
                "SELECT 1 FROM agent_start_operations WHERE agent_id=%(id)s AND operation_id=%(op)s "
                "AND state='pending' FOR UPDATE",
                {"id": operation.agent_id, "op": operation.operation_id},
            )
            is not None
        )

    async def reserve(self, db: DbConnection, operation: AgentStartOperation, *, due_only: bool) -> bool:
        if not await self.lock_pending(db, operation):
            return False
        await self.initialize(db, operation)
        return (
            await db.fetchone(
                "UPDATE agent_start_retry_metadata SET attempts=attempts+1,last_attempt_at=statement_timestamp(),"
                "next_retry_at=statement_timestamp()+make_interval(secs => CASE WHEN attempts >= 5 THEN 300 "
                "ELSE 10 * (1 << attempts) END),updated_at=statement_timestamp() WHERE operation_id=%(op)s "
                "AND (NOT %(due)s OR next_retry_at <= statement_timestamp()) RETURNING operation_id",
                {"op": operation.operation_id, "due": due_only},
            )
            is not None
        )

    async def fail(self, db: DbConnection, operation: AgentStartOperation, code: StartFailureCode) -> None:
        if not await self.lock_pending(db, operation):
            return
        await db.execute(
            "UPDATE agent_start_retry_metadata SET last_failure_code=%(code)s,updated_at=now(),"
            "next_retry_at=CASE WHEN %(code)s IN ('ownership_conflict','configuration_failed') "
            "THEN NULL ELSE next_retry_at END WHERE operation_id=%(op)s",
            {"op": operation.operation_id, "code": code},
        )

    async def defer(self, db: DbConnection, operation: AgentStartOperation, *, upgrade: bool) -> None:
        if not await self.lock_pending(db, operation):
            return
        await self.initialize(db, operation)
        await db.execute(
            "UPDATE agent_start_retry_metadata SET next_retry_at=statement_timestamp()+interval '10 seconds',"
            "last_failure_code=CASE WHEN %(upgrade)s THEN 'upgrade_in_progress' ELSE last_failure_code END,"
            "updated_at=now() WHERE operation_id=%(op)s",
            {"op": operation.operation_id, "upgrade": upgrade},
        )


agent_start_retry_dao = AgentStartRetryDAO()
