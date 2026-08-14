from __future__ import annotations

import importlib
import json
import uuid
from types import ModuleType
from typing import Final

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
    "web_search": "web_search",
    "execute_code": "execute_code",
    "execute_code_e2b": "execute_code",
}


def _mcp_tools_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.mcp_tools")


def _okr_read_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_read")


def _okr_write_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_write")


def _okr_reports_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_reports")


def _deploy_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy")


def _deploy_ops_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy_ops")


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
        if tool_name == "web_search":
            return await agent_tools._web_search(arguments, agent_id)
        if tool_name == "jina_search":
            return await agent_tools._jina_search(arguments)
        if tool_name == "read_webpage":
            return await agent_tools._read_webpage(arguments)
        if tool_name == "exa_search":
            return await agent_tools._exa_search(arguments, agent_id)
        if tool_name == "duckduckgo_search":
            return await agent_tools._duckduckgo_search_tool(arguments)
        if tool_name == "tavily_search":
            return await agent_tools._tavily_search_tool(arguments, agent_id)
        if tool_name == "google_search":
            return await agent_tools._google_search_tool(arguments, agent_id)
        if tool_name == "bing_search":
            return await agent_tools._bing_search_tool(arguments, agent_id)
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
    if not isinstance(tool_name, str):
        tool_name = str(tool_name or "")
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
    if action_type:
        try:
            from app.dao.agent_dao import agent_dao
            from app.services.autonomy_service import autonomy_service

            agent = await agent_dao.get(agent_id)
            if agent:
                result_check = await autonomy_service.check_and_enforce(
                    None,
                    agent,
                    action_type,
                    {"tool": tool_name, "args": str(arguments)[:200], "requested_by": str(user_id)},
                )
                if not result_check.get("allowed"):
                    level = result_check.get("level", "L3")
                    logger.info(f"[Autonomy] Tool {tool_name} denied, level: {level}")
                    if level == "L3":
                        return f"⏳ This action requires approval. An approval request has been sent. Please wait for approval before retrying. (Approval ID: {result_check.get('approval_id', 'N/A')})"
                    return f"❌ Action denied: {result_check.get('message', 'unknown reason')}"
        except Exception as error:
            logger.exception(f"[Autonomy] Check failed: {error}")
            return f"⚠️ Autonomy check failed ({error}). Operation blocked for safety. Please retry or contact admin."
    if tool_name.startswith("agentbay_"):
        arguments["_session_id"] = session_id

        from app.api.agentbay_control import is_session_locked

        if is_session_locked(str(agent_id), session_id):
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
        elif tool_name == "manage_tasks":
            result = await agent_tools._manage_tasks(agent_id, user_id, workspace_root, arguments)
        elif tool_name == "send_platform_message":
            result = await agent_tools._send_platform_message(agent_id, arguments)
        elif tool_name == "send_channel_message":
            result = await agent_tools._send_channel_message(agent_id, arguments)
        elif tool_name == "send_channel_file":
            file_path_value = arguments.get("file_path")
            file_path = file_path_value.strip() if isinstance(file_path_value, str) else ""
            if not file_path:
                result = "Error: file_path is required"
            else:
                from app.services.agent_tool_exec.channel_files import _send_channel_file

                result = await agent_tools._run_with_temp_workspace(
                    agent_id,
                    agent_tenant_id,
                    lambda temp_ws: _send_channel_file(agent_id, temp_ws, arguments),
                    paths=[file_path],
                )
        elif tool_name == "plaza_get_new_posts":
            result = await agent_tools._plaza_get_new_posts(agent_id, arguments)
        elif tool_name == "plaza_create_post":
            result = await agent_tools._plaza_create_post(agent_id, arguments)
        elif tool_name == "plaza_add_comment":
            result = await agent_tools._plaza_add_comment(agent_id, arguments)
        elif tool_name in ("execute_code", "execute_code_e2b"):
            logger.info(f"[DirectTool] Executing code ({tool_name}) with arguments: {arguments}")
            result = await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._execute_code(
                    agent_id, temp_ws, arguments, tool_name=tool_name, on_output=on_output
                ),
                sync_back=True,
            )
        elif tool_name == "upload_image":
            file_path_value = arguments.get("file_path")
            file_path = file_path_value.strip() if isinstance(file_path_value, str) else ""
            result = await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._upload_image(agent_id, temp_ws, arguments),
                paths=agent_tools._non_empty_paths(file_path),
            )
        elif tool_name == "generate_image_siliconflow":
            result = await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._generate_image(agent_id, temp_ws, arguments, "siliconflow"),
                sync_back=True,
            )
        elif tool_name == "generate_image_openai":
            result = await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._generate_image(agent_id, temp_ws, arguments, "openai"),
                sync_back=True,
            )
        elif tool_name == "generate_image_google":
            result = await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._generate_image(agent_id, temp_ws, arguments, "google"),
                sync_back=True,
            )
        elif tool_name == "generate_image_custom":
            result = await agent_tools._run_with_temp_workspace(
                agent_id,
                agent_tenant_id,
                lambda temp_ws: agent_tools._generate_image(agent_id, temp_ws, arguments, "custom"),
                sync_back=True,
            )
        elif tool_name == "discover_resources":
            result = await _mcp_tools_module()._discover_resources(agent_id, arguments)
        elif tool_name == "import_mcp_server":
            result = await _mcp_tools_module()._import_mcp_server(agent_id, arguments)
        elif tool_name in ("send_email", "read_emails", "reply_email"):
            result = await agent_tools._handle_email_tool(tool_name, agent_id, workspace_root, arguments)
        elif tool_name == "publish_page":
            result = await agent_tools._publish_page(agent_id, user_id, workspace_root, arguments)
        elif tool_name == "list_published_pages":
            result = await agent_tools._list_published_pages(agent_id)
        elif tool_name == "search_clawhub":
            result = await agent_tools._search_clawhub(agent_id, arguments)
        elif tool_name == "install_skill":
            result = await agent_tools._install_skill(agent_id, workspace_root, arguments)
        elif tool_name == "get_okr":
            result = await _okr_read_module()._get_okr(agent_id, arguments)
        elif tool_name == "get_my_okr":
            result = await _okr_read_module()._get_my_okr(agent_id, arguments)
        elif tool_name == "update_kr_content":
            result = await _okr_write_module()._update_kr_content(agent_id, user_id, arguments)
        elif tool_name == "update_kr_progress":
            result = await _okr_write_module()._update_kr_progress(agent_id, user_id, arguments)
        elif tool_name == "collect_okr_progress":
            result = await _okr_reports_module()._collect_okr_progress(agent_id)
        elif tool_name == "generate_okr_report":
            result = await _okr_reports_module()._generate_okr_report(agent_id, arguments)
        elif tool_name == "get_okr_settings":
            result = await _okr_read_module()._get_okr_settings_tool(agent_id)
        elif tool_name == "create_objective":
            result = await _okr_write_module()._create_objective(agent_id, user_id, arguments)
        elif tool_name == "create_key_result":
            result = await _okr_write_module()._create_key_result(agent_id, user_id, arguments)
        elif tool_name == "update_objective":
            result = await _okr_write_module()._update_objective(agent_id, user_id, arguments)
        elif tool_name == "update_any_kr_progress":
            result = await _okr_write_module()._update_any_kr_progress(agent_id, user_id, arguments)
        elif tool_name == "generate_monthly_okr_report":
            result = await _okr_reports_module()._generate_monthly_okr_report(agent_id)
        elif tool_name == "upsert_member_daily_report":
            result = await _okr_reports_module()._upsert_member_daily_report(agent_id, arguments)
        elif tool_name == "vercel_deploy":
            result = await _deploy_module()._vercel_deploy(agent_id, workspace_root, arguments)
        elif tool_name == "vercel_list_deployments":
            result = await _deploy_module()._vercel_list_deployments(agent_id, arguments)
        elif tool_name == "vercel_get_deploy_logs":
            result = await _deploy_module()._vercel_get_deploy_logs(agent_id, arguments)
        elif tool_name == "vercel_set_env":
            result = await _deploy_ops_module()._vercel_set_env(agent_id, arguments)
        elif tool_name == "vercel_manage_domain":
            result = await _deploy_ops_module()._vercel_manage_domain(agent_id, arguments)
        elif tool_name == "neon_create_database":
            result = await _deploy_ops_module()._neon_create_database(agent_id, arguments)
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
            and isinstance(result, str)
            and result.startswith("❌")
        ):
            try:
                from app.dao.chat_dao import chat_message_dao

                if user_id is not None:
                    _ = await chat_message_dao.insert_message(
                        agent_id=agent_id,
                        user_id=user_id,
                        role="assistant",
                        content=(
                            f"⚠️ [System notice] Digital employee tool call failed.\n"
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
