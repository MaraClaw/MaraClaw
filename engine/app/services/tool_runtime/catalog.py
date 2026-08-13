"""Dynamic LLM tool catalog assembly with facade-supplied dependencies."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from app.core.logging import logger
from app.core.tool_types import ToolDefinition
from app.dao import agent_dao, agent_tool_dao, tenant_dao, tool_dao
from app.db.pool import get_pool
from app.db.session import connection_ctx, get_connection


@dataclass(frozen=True, slots=True)
class CatalogDependencies:
    """Facade collaborators captured for one catalog assembly request."""

    session_factory: Callable[[], AbstractAsyncContextManager[Any]]
    agent_has_feishu: Callable[[uuid.UUID], Awaitable[bool]]
    agent_has_any_channel: Callable[[uuid.UUID], Awaitable[bool]]
    get_computer_os_type: Callable[[uuid.UUID], Awaitable[str]]
    patch_computer_tool_descriptions: Callable[[list[ToolDefinition], str], list[ToolDefinition]]
    strip_a2a_msg_type: Callable[[list[ToolDefinition]], list[ToolDefinition]]
    always_core_tools: list[ToolDefinition]
    feishu_tools: list[ToolDefinition]
    channel_tools: list[ToolDefinition]


async def _channel_presence(agent_id: uuid.UUID) -> tuple[bool, bool]:
    """Return (has_feishu, has_any_configured_channel) in one query."""
    try:
        async with connection_ctx() as db:
            row = await db.fetchone(
                """
                SELECT
                  EXISTS(
                    SELECT 1 FROM channel_configs
                    WHERE agent_id = %(agent_id)s
                      AND channel_type = 'feishu'
                      AND is_configured IS TRUE
                  ) AS has_feishu,
                  EXISTS(
                    SELECT 1 FROM channel_configs
                    WHERE agent_id = %(agent_id)s AND is_configured IS TRUE
                  ) AS has_any
                """,
                {"agent_id": agent_id},
            )
            if not row:
                return False, False
            return bool(row["has_feishu"]), bool(row["has_any"])
    except Exception:
        return False, False


async def _resolve_channel_presence(
    agent_id: uuid.UUID,
    dependencies: CatalogDependencies,
) -> tuple[bool, bool]:
    """One EXISTS pair when both lookups are the catalog defaults."""
    feishu_fn = dependencies.agent_has_feishu
    any_fn = dependencies.agent_has_any_channel
    if getattr(feishu_fn, "_uses_catalog_channel_presence", False) and getattr(
        any_fn, "_uses_catalog_channel_presence", False
    ):
        return await _channel_presence(agent_id)
    return await feishu_fn(agent_id), await any_fn(agent_id)


async def agent_has_feishu(
    agent_id: uuid.UUID,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> bool:
    """Check whether an agent has a configured Feishu channel."""
    del session_factory
    has_feishu, _has_any = await _channel_presence(agent_id)
    return has_feishu


async def agent_has_any_channel(
    agent_id: uuid.UUID,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
) -> bool:
    """Check whether an agent has any configured external channel."""
    del session_factory
    _has_feishu, has_any = await _channel_presence(agent_id)
    return has_any


agent_has_feishu._uses_catalog_channel_presence = True
agent_has_any_channel._uses_catalog_channel_presence = True


def strip_a2a_msg_type(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """Copy only the synchronous A2A tool while removing its async message mode."""
    result = []
    for tool in tools:
        function = tool["function"]
        if function["name"] == "send_message_to_agent":
            tool = copy.deepcopy(tool)
            function = tool["function"]
            function["description"] = (
                "Send a message to a digital employee colleague and receive their reply synchronously."
            )
            parameters = function["parameters"]
            properties = parameters["properties"]
            properties.pop("msg_type", None)
            required = parameters.get("required", [])
            if "msg_type" in required:
                parameters["required"] = [item for item in required if item != "msg_type"]
        result.append(tool)
    return result


async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    *,
    dependencies: CatalogDependencies,
) -> list[ToolDefinition]:
    """Build the visible LLM catalog while preserving legacy fallback semantics."""
    if get_connection() is not None:
        return await _get_agent_tools_for_llm_bound(agent_id, dependencies=dependencies)
    try:
        get_pool()
    except RuntimeError:
        return await _get_agent_tools_for_llm_bound(agent_id, dependencies=dependencies)
    async with connection_ctx():
        return await _get_agent_tools_for_llm_bound(agent_id, dependencies=dependencies)


async def _get_agent_tools_for_llm_bound(
    agent_id: uuid.UUID,
    *,
    dependencies: CatalogDependencies,
) -> list[ToolDefinition]:
    has_feishu, has_any_channel = await _resolve_channel_presence(agent_id, dependencies)
    always_tools = (
        dependencies.always_core_tools
        + (dependencies.feishu_tools if has_feishu else [])
        + (dependencies.channel_tools if has_any_channel else [])
    )

    a2a_async_enabled = False
    is_system_agent = False
    agent_tenant_id = None
    try:
        agent = await agent_dao.get(agent_id)
        agent_tenant_id = agent.tenant_id if agent else None
        is_system_agent = bool(agent and agent.is_system)
        if agent_tenant_id:
            tenant = await tenant_dao.get(agent_tenant_id)
            if tenant:
                a2a_async_enabled = bool(getattr(tenant, "a2a_async_enabled", False))
    except Exception:
        logger.debug("[Tools] tenant flags unavailable; using synchronous A2A fallback")

    computer_os_type = await dependencies.get_computer_os_type(agent_id)
    try:
        assignments_list = await agent_tool_dao.list_for_agent(agent_id)
        assignments = {str(assignment.tool_id): assignment for assignment in assignments_list}
        assigned_tool_ids = [assignment.tool_id for assignment in assignments_list]
        all_tools = await tool_dao.list_enabled_for_agent_catalog(
            agent_tenant_id=agent_tenant_id,
            assigned_tool_ids=assigned_tool_ids,
        )

        result: list[ToolDefinition] = []
        db_tool_names = set()
        explicitly_disabled_names = set()
        for tool in all_tools:
            assignment = assignments.get(str(tool.id))
            enabled = assignment.enabled if assignment is not None else tool.is_default
            if not enabled:
                if assignment and not assignment.enabled:
                    explicitly_disabled_names.add(tool.name)
                continue
            if tool.category == "feishu" and not has_feishu:
                continue
            if (tool.config or {}).get("okr_agent_only") and not is_system_agent:
                continue
            if tool.name in db_tool_names:
                logger.warning(f"[Tools] Duplicate tool name '{tool.name}' found in DB. Skipping to avoid LLM error.")
                continue
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters_schema or {"type": "object", "properties": {}},
                    },
                }
            )
            db_tool_names.add(tool.name)

        if result:
            for tool in always_tools:
                function_name = tool["function"]["name"]
                if function_name not in db_tool_names and function_name not in explicitly_disabled_names:
                    result.append(tool)
            result = dependencies.patch_computer_tool_descriptions(result, computer_os_type)
            return dependencies.strip_a2a_msg_type(result) if not a2a_async_enabled else result
        raise ValueError("No tools found for agent in DB")
    except Exception as error:
        logger.error(f"[Tools] DB load failed, using fallback: {error}")

    fallback = dependencies.patch_computer_tool_descriptions(always_tools, computer_os_type)
    return dependencies.strip_a2a_msg_type(fallback) if not a2a_async_enabled else fallback
