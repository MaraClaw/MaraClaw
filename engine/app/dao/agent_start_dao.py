from uuid import UUID, uuid4

from app.dao.agent_start_retry_dao import START_RETRY_METADATA_DDL
from app.db.connection import DbConnection
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation

# Parent integration copies this additive fresh-schema DDL into the shared baseline.
START_OPERATIONS_DDL = (
    """
CREATE TABLE IF NOT EXISTS agent_start_operations (
    agent_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('pending', 'succeeded', 'cancelled')),
    creator_id uuid NOT NULL,
    tenant_id uuid,
    expected_container_id text,
    candidate_id text,
    candidate_port integer,
    cleanup_complete boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_start_operations_pending_idx
    ON agent_start_operations(updated_at) WHERE state = 'pending';
"""
    + START_RETRY_METADATA_DDL
)

ELIGIBLE_SQL = """
NOT a.setup_pending AND NOT a.is_expired AND (a.expires_at IS NULL OR a.expires_at > now())
AND EXISTS (SELECT 1 FROM users u JOIN identities i ON i.id=u.identity_id
    WHERE u.id=a.creator_id AND u.is_active AND i.is_active
    AND u.tenant_id IS NOT DISTINCT FROM a.tenant_id)
AND (a.tenant_id IS NULL OR EXISTS (SELECT 1 FROM tenants t WHERE t.id=a.tenant_id AND t.is_active))
AND NOT EXISTS (SELECT 1 FROM company_transfer_runtime_cleanup c WHERE c.agent_id=a.id AND c.state='pending')
"""


class AgentStartDAO:
    async def eligible(self, db: DbConnection, agent_id: UUID) -> AgentRecord | None:
        row = await db.fetchone(
            f"SELECT a.* FROM agents a WHERE a.id=%(id)s AND {ELIGIBLE_SQL} FOR UPDATE OF a",
            {"id": agent_id},
        )
        return AgentRecord.from_row(row) if row is not None else None

    async def begin(self, db: DbConnection, agent: AgentRecord) -> AgentStartOperation:
        row = await db.fetchone(
            "INSERT INTO agent_start_operations (agent_id,operation_id,state,creator_id,tenant_id,expected_container_id) "
            "VALUES (%(id)s,%(op)s,'pending',%(creator)s,%(tenant)s,%(container)s) "
            "ON CONFLICT (agent_id) DO UPDATE SET "
            "operation_id=EXCLUDED.operation_id,state='pending',creator_id=EXCLUDED.creator_id, "
            "tenant_id=EXCLUDED.tenant_id,expected_container_id=EXCLUDED.expected_container_id,"
            "candidate_id=NULL,candidate_port=NULL,cleanup_complete=false,updated_at=now() "
            "RETURNING agent_id,operation_id,state",
            {
                "id": agent.id,
                "op": uuid4(),
                "creator": agent.creator_id,
                "tenant": agent.tenant_id,
                "container": agent.container_id,
            },
        )
        if row is None:
            raise RuntimeError("Start operation insert returned no record")
        return AgentStartOperation.from_row(row)

    async def get(self, db: DbConnection, agent_id: UUID) -> AgentStartOperation | None:
        row = await db.fetchone("SELECT * FROM agent_start_operations WHERE agent_id=%(id)s", {"id": agent_id})
        return AgentStartOperation.from_row(row) if row is not None else None

    async def pending(self, db: DbConnection) -> list[AgentStartOperation]:
        rows = await db.fetchall(
            "(SELECT o.* FROM agent_start_operations o LEFT JOIN agent_start_retry_metadata r "
            "ON r.operation_id=o.operation_id WHERE o.state='pending' "
            "AND (r.operation_id IS NULL OR r.next_retry_at <= now()) "
            "ORDER BY COALESCE(r.next_retry_at,o.updated_at),o.agent_id LIMIT 8) UNION ALL "
            "(SELECT * FROM agent_start_operations WHERE state='cancelled' AND NOT cleanup_complete "
            "ORDER BY updated_at,agent_id LIMIT 8)"
        )
        return [AgentStartOperation.from_row(row) for row in rows]

    async def cancel(self, db: DbConnection, agent_id: UUID) -> None:
        await db.execute(
            "UPDATE agent_start_operations SET state='cancelled',updated_at=now() "
            "WHERE agent_id=%(id)s AND state='pending'",
            {"id": agent_id},
        )

    async def active_agent(self, db: DbConnection, operation: AgentStartOperation) -> AgentRecord | None:
        row = await db.fetchone(
            "SELECT a.* FROM agents a JOIN agent_start_operations o ON o.agent_id=a.id "
            "WHERE o.operation_id=%(op)s AND o.state='pending' "
            "AND a.creator_id=o.creator_id AND a.tenant_id IS NOT DISTINCT FROM o.tenant_id "
            "AND a.container_id IS NOT DISTINCT FROM o.expected_container_id "
            f"AND {ELIGIBLE_SQL} FOR UPDATE OF a",
            {"op": operation.operation_id},
        )
        return AgentRecord.from_row(row) if row is not None else None

    async def publish(self, db: DbConnection, operation: AgentStartOperation, agent: AgentRecord) -> bool:
        row = await db.fetchone(
            "UPDATE agents a SET status=%(status)s::agent_status_enum,container_id=%(container)s,container_port=%(port)s,"
            "last_active_at=now(),api_key_hash=%(key)s,updated_at=now() "
            "FROM agent_start_operations o WHERE a.id=%(id)s AND o.agent_id=a.id "
            "AND o.operation_id=%(op)s AND o.state='pending' "
            "AND a.creator_id=o.creator_id AND a.tenant_id IS NOT DISTINCT FROM o.tenant_id "
            "AND a.container_id IS NOT DISTINCT FROM o.expected_container_id "
            f"AND {ELIGIBLE_SQL} RETURNING a.id",
            {
                "id": agent.id,
                "op": operation.operation_id,
                "status": agent.status,
                "container": agent.container_id,
                "port": agent.container_port,
                "key": agent.api_key_hash,
            },
        )
        if row is None:
            return False
        await db.execute(
            "UPDATE agent_start_operations SET state='succeeded',candidate_id=%(container)s,"
            "candidate_port=%(port)s,updated_at=now() WHERE operation_id=%(op)s",
            {"op": operation.operation_id, "container": agent.container_id, "port": agent.container_port},
        )
        return True


agent_start_dao = AgentStartDAO()
