from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

from app.records.agent import AgentRecord
from app.records.agent_start import AgentStartOperation
from test_agent_create_departments import CreationStore


@dataclass(slots=True)
class AssistantStartStore:
    store: CreationStore
    operations: dict[UUID, AgentStartOperation] = field(default_factory=dict)
    expected: dict[UUID, AgentRecord] = field(default_factory=dict)
    candidates: dict[UUID, AgentRecord] = field(default_factory=dict)

    def _agent(self, agent_id: UUID) -> AgentRecord | None:
        return next((replace(row) for row in self.store.agent_rows if row.id == agent_id), None)

    async def begin(self, agent_id: UUID) -> tuple[AgentStartOperation, AgentRecord] | None:
        assert not self.store.in_transaction
        agent = self._agent(agent_id)
        if agent is None:
            return None
        current = self.operations.get(agent_id)
        if current is not None and await self.recover(current) is not None:
            return current, agent
        operation = AgentStartOperation(agent_id, uuid4(), "pending")
        self.operations[agent_id] = operation
        self.expected[agent_id] = replace(agent)
        return operation, agent

    async def recover(self, operation: AgentStartOperation) -> AgentRecord | None:
        if self.operations.get(operation.agent_id) != operation or operation.state != "pending":
            return None
        agent = self._agent(operation.agent_id)
        expected = self.expected[operation.agent_id]
        if agent is None or (agent.creator_id, agent.tenant_id, agent.container_id) != (
            expected.creator_id,
            expected.tenant_id,
            expected.container_id,
        ):
            self.operations[operation.agent_id] = replace(operation, state="cancelled")
            return None
        return agent

    async def observe(self, operation_id: UUID, candidate: AgentRecord) -> bool:
        operation = self.operations.get(candidate.id)
        if (
            operation is None
            or operation.operation_id != operation_id
            or operation.state not in ("pending", "cancelled")
        ):
            return False
        self.candidates[operation_id] = replace(candidate)
        return operation.state == "pending"

    async def publish(self, operation: AgentStartOperation, agent: AgentRecord) -> bool:
        assert not self.store.in_transaction
        current = await self.recover(operation)
        if current is None:
            return False
        await self.store.update_agent(
            db_obj=current,
            obj_in={
                "status": agent.status,
                "container_id": agent.container_id,
                "container_port": agent.container_port,
                "api_key_hash": agent.api_key_hash,
                "last_active_at": agent.last_active_at,
            },
        )
        self.operations[agent.id] = replace(operation, state="succeeded")
        return True
