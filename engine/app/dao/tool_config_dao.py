from uuid import UUID, uuid4

from app.core.acl_locks import lock_agent
from app.dao.tool_dao import agent_tool_dao
from app.db.session import connection_ctx
from app.db.types import as_jsonb
from app.records.agent import AgentRecord
from app.records.tool import ToolRecord
from app.schemas.tool_management import ConfigOperations, SavedToolReference

TENANT_TOOL_CONFIG_PREFIX = "tool_config:"


def tenant_tool_config_key(tool_name: str) -> str:
    return f"{TENANT_TOOL_CONFIG_PREFIX}{tool_name}"


async def lock_agent_config(agent: AgentRecord, tool_id: UUID) -> None:
    async with connection_ctx() as db:
        await lock_agent(db, agent.id, agent.tenant_id)
        await db.execute(
            "SELECT id FROM agent_tools WHERE agent_id=%(agent)s AND tool_id=%(tool)s FOR UPDATE",
            {"agent": agent.id, "tool": tool_id},
        )


async def lock_tool_config(tool_id: UUID) -> None:
    async with connection_ctx() as db:
        await db.execute("SELECT id FROM tools WHERE id=%(id)s FOR UPDATE", {"id": tool_id})


async def patch_config_layer(tool: ToolRecord, reference: SavedToolReference, operations: ConfigOperations) -> None:
    async with connection_ctx() as db:
        params = {"set": as_jsonb(operations.set), "unset": operations.unset, "tool_id": tool.id}
        if reference.agent_id is not None:
            assignment = await agent_tool_dao.get_assignment(reference.agent_id, tool.id)
            if assignment is None:
                await db.execute(
                    "INSERT INTO agent_tools (id, agent_id, tool_id, enabled, config) "
                    "VALUES (%(id)s, %(agent_id)s, %(tool_id)s, true, %(set)s)",
                    {**params, "id": uuid4(), "agent_id": reference.agent_id},
                )
            else:
                await db.execute(
                    "UPDATE agent_tools SET config = (COALESCE(config::jsonb, '{}'::jsonb) - %(unset)s::text[]) "
                    "|| %(set)s::jsonb WHERE id = %(id)s",
                    {**params, "id": assignment.id},
                )
        elif tool.source == "builtin":
            await db.execute(
                "INSERT INTO tenant_settings (tenant_id, key, value) "
                "VALUES (%(tenant_id)s, %(key)s, jsonb_build_object('config', %(set)s::jsonb)) "
                "ON CONFLICT (tenant_id, key) DO UPDATE SET value = jsonb_set(tenant_settings.value, '{config}', "
                "(COALESCE(tenant_settings.value->'config', '{}'::jsonb) - %(unset)s::text[]) || %(set)s::jsonb), "
                "updated_at = now()",
                {**params, "tenant_id": reference.tenant_id, "key": tenant_tool_config_key(tool.name)},
            )
        else:
            await db.execute(
                "UPDATE tools SET config = (COALESCE(config::jsonb, '{}'::jsonb) - %(unset)s::text[]) "
                "|| %(set)s::jsonb, updated_at = now() WHERE id = %(tool_id)s",
                params,
            )
