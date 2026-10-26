from uuid import UUID

from app.db.connection import DbConnection
from app.schemas.agent_lifecycle import AgentLifecycleStartOut


class AgentLifecycleProjectionDAO:
    async def start(self, db: DbConnection, agent_id: UUID) -> AgentLifecycleStartOut | None:
        row = await db.fetchone(
            "SELECT o.operation_id,o.state,COALESCE(r.attempts,0) AS attempts,"
            "r.last_attempt_at,r.next_retry_at,r.last_failure_code FROM agent_start_operations o "
            "LEFT JOIN agent_start_retry_metadata r ON r.operation_id=o.operation_id WHERE o.agent_id=%(id)s",
            {"id": agent_id},
        )
        return AgentLifecycleStartOut.model_validate(row) if row is not None else None


agent_lifecycle_projection_dao = AgentLifecycleProjectionDAO()
