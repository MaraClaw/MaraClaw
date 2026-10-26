from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from app.core.row_memo import memo_drop
from app.dao.agent_start_dao import agent_start_dao
from app.dao.agent_start_retry_dao import agent_start_retry_dao
from app.db.connection import DbConnection
from app.db.pool import get_pool
from app.db.session import transaction
from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from app.records.agent_start_retry import StartFailureCode


@asynccontextmanager
async def committed_start_connection() -> AsyncGenerator[DbConnection]:
    # An explicit new checkout owns its commit even inside a request's connection_ctx.
    async with get_pool().connection() as raw, transaction(db=DbConnection(raw)) as db:
        yield db


class PostgresStartStore:
    async def begin(self, agent_id: UUID) -> tuple[AgentStartOperation, AgentRecord] | None:
        async with committed_start_connection() as db:
            agent = await agent_start_dao.eligible(db, agent_id)
            if agent is None:
                return None
            operation = await agent_start_dao.get(db, agent_id)
            if operation is not None and operation.state == "pending":
                active = await agent_start_dao.active_agent(db, operation)
                if active is not None:
                    return operation, active
            operation = await agent_start_dao.begin(db, agent)
            await agent_start_retry_dao.initialize(db, operation)
        return operation, agent

    async def recover(self, operation: AgentStartOperation) -> AgentRecord | None:
        async with committed_start_connection() as db:
            agent = await agent_start_dao.active_agent(db, operation)
            current = await agent_start_dao.get(db, operation.agent_id)
            if agent is None or current != operation or operation.state != "pending":
                if current == operation:
                    await agent_start_dao.cancel(db, operation.agent_id)
                return None
            return agent

    async def outstanding(self) -> list[AgentStartOperation]:
        async with committed_start_connection() as db:
            return await agent_start_dao.pending(db)

    async def reserve(self, operation: AgentStartOperation, *, due_only: bool = False) -> bool:
        async with committed_start_connection() as db:
            if await agent_start_dao.active_agent(db, operation) is None:
                return False
            return await agent_start_retry_dao.reserve(db, operation, due_only=due_only)

    async def fail(self, operation: AgentStartOperation, code: StartFailureCode) -> None:
        async with committed_start_connection() as db:
            await agent_start_retry_dao.fail(db, operation, code)

    async def defer(self, operation: AgentStartOperation, *, upgrade: bool = False) -> None:
        async with committed_start_connection() as db:
            await agent_start_retry_dao.defer(db, operation, upgrade=upgrade)

    async def rotate_cleanup(self, operation: AgentStartOperation) -> None:
        async with committed_start_connection() as db:
            await db.execute(
                "UPDATE agent_start_operations SET updated_at=now() WHERE operation_id=%(op)s AND state='cancelled'",
                {"op": operation.operation_id},
            )

    async def cancel(self, agent_id: UUID) -> None:
        async with committed_start_connection() as db:
            await agent_start_dao.cancel(db, agent_id)

    async def observe(self, operation_id: UUID, candidate: AgentRecord) -> bool:
        async with committed_start_connection() as db:
            state = await db.fetchval(
                "UPDATE agent_start_operations SET candidate_id=%(container)s,candidate_port=%(port)s,updated_at=now() "
                "WHERE operation_id=%(op)s AND state IN ('pending','cancelled') RETURNING state",
                {"op": operation_id, "container": candidate.container_id, "port": candidate.container_port},
            )
        return state == "pending"

    async def publish(self, operation: AgentStartOperation, agent: AgentRecord) -> bool:
        async with committed_start_connection() as db:
            if await agent_start_dao.eligible(db, agent.id) is None:
                return False
            published = await agent_start_dao.publish(db, operation, agent)
        memo_drop("agent", agent.id)
        return published

    async def published(self, operation: AgentStartOperation, candidate: AgentRecord) -> bool:
        async with committed_start_connection() as db:
            row = await db.fetchone(
                "SELECT 1 FROM agents a JOIN agent_start_operations o ON o.agent_id=a.id "
                "WHERE a.id=%(id)s AND o.operation_id=%(op)s AND o.state='succeeded' "
                "AND a.status=%(status)s::agent_status_enum AND a.container_id IS NOT DISTINCT FROM %(container)s",
                {
                    "id": candidate.id,
                    "op": operation.operation_id,
                    "status": candidate.status,
                    "container": candidate.container_id,
                },
            )
            return row is not None


start_store = PostgresStartStore()
