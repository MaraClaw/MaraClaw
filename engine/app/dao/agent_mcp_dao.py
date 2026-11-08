"""Agent-owned MCP desired state, including durable deletion ownership."""

from uuid import UUID

from app.db.session import connection_ctx
from app.records.agent_mcp import AgentMCPServerRecord


class AgentMCPServerDAO:
    async def list_for_agent(self, agent_id: UUID) -> list[AgentMCPServerRecord]:
        async with connection_ctx() as db:
            rows = await db.fetchall(
                "SELECT agent_id,name,definition_ciphertext FROM agent_mcp_servers "
                "WHERE agent_id=%(agent)s ORDER BY name",
                {"agent": agent_id},
            )
            return [AgentMCPServerRecord.from_row(row) for row in rows]

    async def save(self, record: AgentMCPServerRecord) -> None:
        async with connection_ctx() as db:
            await db.execute(
                "INSERT INTO agent_mcp_servers(agent_id,name,definition_ciphertext) "
                "VALUES(%(agent)s,%(name)s,%(ciphertext)s) ON CONFLICT(agent_id,name) DO UPDATE "
                "SET definition_ciphertext=EXCLUDED.definition_ciphertext,updated_at=now()",
                {"agent": record.agent_id, "name": record.name, "ciphertext": record.definition_ciphertext},
            )


agent_mcp_server_dao = AgentMCPServerDAO()
