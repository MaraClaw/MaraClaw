from __future__ import annotations

import importlib
import json
import uuid
from typing import Final, Protocol, TypeIs, cast

from app.core.json_types import json_as_bool, json_as_str, json_as_str_or
from app.core.logging import logger
from app.services import agent_tools
from app.services.agent_tool_exec.registry import (
    ToolArguments,
    ToolExecutionContext,
    ToolOutputCallback,
    use_execution_context,
)
from app.services.llm.finish import FINISH_TOOL_NAME

_TOOL_AUTONOMY_MAP: Final = {
    "write_file": "write_workspace_files",
    "move_file": "write_workspace_files",
    "delete_file": "delete_files",
    "send_feishu_message": "send_feishu_message",
    "send_message_to_agent": "send_message_to_agent",
    "send_file_to_agent": "send_file_to_agent",
    "execute_code": "execute_code",
    "execute_code_e2b": "execute_code",
}


class _McpToolsModule(Protocol):
    async def _execute_mcp_tool(
        self, tool_name: str, arguments: ToolArguments, agent_id: uuid.UUID | None = None
    ) -> str: ...


def _is_mcp_tools_module(value: object) -> TypeIs[_McpToolsModule]:
    return callable(getattr(value, "_execute_mcp_tool", None))


def _load_exec_module(name: str) -> object:
    return importlib.import_module(name)


def _mcp_tools_module() -> _McpToolsModule:
    module = _load_exec_module("app.services.agent_tool_exec.mcp_tools")
    if not _is_mcp_tools_module(module):
        raise TypeError("mcp_tools module is missing required handlers")
    return module


async def _execute_tool_direct(
    tool_name: str,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
) -> str:
    """Execute a tool directly, bypassing autonomy checks."""
    agent_tenant_id = await agent_tools._get_agent_tenant_id(agent_id)
    workspace_root = agent_tools._agent_workspace_root(agent_id)
    try:
        if tool_name in {"delete_file", "write_file", "move_file", "edit_file"}:
            return await agent_tools._execute_workspace_mutation(
                tool_name,
                arguments,
                agent_id=agent_id,
                base_dir=workspace_root,
                session_id=None,
            )
        if tool_name in ("execute_code", "execute_code_e2b"):
            logger.info(f"[DirectTool] Executing code ({tool_name}) with arguments: {arguments}")
            return await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._execute_code(agent_id, temp_ws, arguments, tool_name=tool_name),
                sync_back=True,
            )
        if tool_name == "read_webpage":
            return await agent_tools._read_webpage(arguments)
        if tool_name == "send_feishu_message":
            return await agent_tools._send_feishu_message(agent_id, arguments)
        if tool_name == "send_message_to_agent":
            return await agent_tools._send_message_to_agent(
                agent_id,
                arguments,
                user_id=None,
                origin_session_id=None,
            )
        if tool_name == "send_file_to_agent":
            return await agent_tools._send_file_to_agent(agent_id, arguments)
        return f"Tool {tool_name} does not support post-approval execution"
    except Exception as error:
        logger.exception(f"[DirectTool] Error executing {tool_name}: {error}")
        return f"Error executing {tool_name}: {error}"


async def execute_tool(
    tool_name: str,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str = "",
    on_output: ToolOutputCallback | None = None,
) -> str:
    """Execute a tool call and return the result as a string."""
    # Provider JSON can carry a non-string name despite the declared type; coerce before sanitizing.
    tool_name_arg = cast(object, tool_name)
    tool_name = tool_name_arg if isinstance(tool_name_arg, str) else str(tool_name_arg or "")
    tool_name = (
        tool_name.replace("`", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .strip()
    )
    if tool_name == FINISH_TOOL_NAME:
        content = arguments.get("content", "")
        return content if isinstance(content, str) else str(content)

    agent_tenant_id = await agent_tools._get_agent_tenant_id(agent_id)
    workspace_root = agent_tools._agent_workspace_root(agent_id)

    action_type = _TOOL_AUTONOMY_MAP.get(tool_name)
    if action_type is not None:
        try:
            from app.dao.agent_dao import agent_dao
            from app.services.autonomy_service import autonomy_service

            agent = await agent_dao.get(agent_id)
            if agent:
                result_check = await autonomy_service.check_and_enforce(
                    agent,
                    action_type,
                    {"tool": tool_name, "args": str(arguments)[:200], "requested_by": str(user_id)},
                )
                if not json_as_bool(result_check.get("allowed")):
                    level = json_as_str_or(result_check.get("level"), "L3")
                    logger.info(f"[Autonomy] Tool {tool_name} denied, level: {level}")
                    if level == "L3":
                        approval_id = json_as_str(result_check.get("approval_id")) or "N/A"
                        return (
                            "⏳ This action requires approval. An approval request has been sent. "
                            + f"Please wait for approval before retrying. (Approval ID: {approval_id})"
                        )
                    return f"❌ Action denied: {json_as_str_or(result_check.get('message'), 'unknown reason')}"
        except Exception as error:
            logger.exception(f"[Autonomy] Check failed: {error}")
            return f"⚠️ Autonomy check failed ({error}). Operation blocked for safety. Please retry or contact admin."
    if tool_name.startswith("agentbay_"):
        arguments["_session_id"] = session_id

        from app.services.take_control_state import TakeControlUnavailableError, is_session_locked

        try:
            locked = await is_session_locked(str(agent_id), session_id)
        except TakeControlUnavailableError:
            return "Take Control state unavailable. Operation blocked for safety; please retry."
        if locked:
            return (
                "⏸️ A human operator is currently controlling this browser session "
                + "(Take Control mode). Please wait for them to finish before retrying "
                + "browser/computer operations."
            )

    try:
        registered_handler = agent_tools.resolve_tool_handler(tool_name)
        if registered_handler is not None:
            with use_execution_context(ToolExecutionContext(tenant_id=agent_tenant_id, workspace_root=workspace_root)):
                handler_result = registered_handler(
                    arguments=arguments,
                    agent_id=agent_id,
                    user_id=user_id,
                    session_id=session_id,
                    on_output=on_output,
                )
                result = handler_result if isinstance(handler_result, str) else await handler_result
        else:
            result = await _mcp_tools_module()._execute_mcp_tool(tool_name, arguments, agent_id=agent_id)

        if tool_name not in ("list_files", "read_file", "read_document"):
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "tool_call",
                f"Called tool {tool_name}: {result[:80]}",
                detail={
                    "tool": tool_name,
                    "args": {key: str(value)[:100] for key, value in arguments.items()},
                    "result": result[:300],
                },
            )
        if (
            session_id
            and tool_name
            in ("send_channel_message", "send_feishu_message", "send_platform_message", "send_message_to_agent")
            and result.startswith("❌")
        ):
            try:
                from app.dao.chat_dao import chat_message_dao

                _ = await chat_message_dao.insert_message(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content=(
                        "⚠️ [System notice] Digital employee tool call failed.\n"
                        + f"Tool: `{tool_name}`\n"
                        + f"Arguments: `{json.dumps(arguments, ensure_ascii=False)}`\n"
                        + f"Error: {result}"
                    ),
                    conversation_id=session_id,
                )
            except Exception as error:
                logger.warning(f"Failed to save tool error message to session: {error}")

        return result
    except Exception as error:
        logger.exception(f"[Tool] Execution failed: {tool_name}")
        return f"Tool execution error ({tool_name}): {type(error).__name__}: {str(error)[:200]}"
