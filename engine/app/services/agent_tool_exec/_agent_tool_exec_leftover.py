"""Register leftover dispatcher families onto TOOL_HANDLERS."""

from __future__ import annotations

import importlib
import uuid

from app.services.agent_tool_exec.registry import ToolArguments, ToolOutputCallback, register


def _agent_tools():
    return importlib.import_module("app.services.agent_tools")


def _mcp_tools():
    return importlib.import_module("app.services.agent_tool_exec.mcp_tools")


def _okr_read():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_read")


def _okr_write():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_write")


def _okr_reports():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_okr_reports")


def _deploy():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy")


def _deploy_ops():
    return importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy_ops")


@register("manage_tasks")
async def manage_tasks(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    tools = _agent_tools()
    return await tools._manage_tasks(agent_id, user_id, tools._agent_workspace_root(agent_id), arguments)


@register("send_platform_message")
async def send_platform_message(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _agent_tools()._send_platform_message(agent_id, arguments)


@register("send_channel_message")
async def send_channel_message(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _agent_tools()._send_channel_message(agent_id, arguments)


@register("send_channel_file")
async def send_channel_file(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    file_path_value = arguments.get("file_path")
    file_path = file_path_value.strip() if isinstance(file_path_value, str) else ""
    if not file_path:
        return "Error: file_path is required"
    from app.services.agent_tool_exec.channel_files import _send_channel_file

    tools = _agent_tools()
    tenant_id = await tools._get_agent_tenant_id(agent_id)
    return await tools._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: _send_channel_file(agent_id, temp_ws, arguments),
        paths=[file_path],
    )


@register("plaza_get_new_posts")
async def plaza_get_new_posts(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _agent_tools()._plaza_get_new_posts(agent_id, arguments)


@register("plaza_create_post")
async def plaza_create_post(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _agent_tools()._plaza_create_post(agent_id, arguments)


@register("plaza_add_comment")
async def plaza_add_comment(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _agent_tools()._plaza_add_comment(agent_id, arguments)


@register("execute_code")
async def execute_code(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id
    tools = _agent_tools()
    tenant_id = await tools._get_agent_tenant_id(agent_id)
    return await tools._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: tools._execute_code(
            agent_id, temp_ws, arguments, tool_name="execute_code", on_output=on_output
        ),
        sync_back=True,
    )


@register("execute_code_e2b")
async def execute_code_e2b(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id
    tools = _agent_tools()
    tenant_id = await tools._get_agent_tenant_id(agent_id)
    return await tools._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: tools._execute_code(
            agent_id, temp_ws, arguments, tool_name="execute_code_e2b", on_output=on_output
        ),
        sync_back=True,
    )


@register("upload_image")
async def upload_image(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    tools = _agent_tools()
    file_path_value = arguments.get("file_path")
    file_path = file_path_value.strip() if isinstance(file_path_value, str) else ""
    tenant_id = await tools._get_agent_tenant_id(agent_id)
    return await tools._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: tools._upload_image(agent_id, temp_ws, arguments),
        paths=tools._non_empty_paths(file_path),
    )


def _register_generate_image(name: str, provider: str) -> None:
    @register(name)
    async def _generate(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        del user_id, session_id, on_output
        tools = _agent_tools()
        tenant_id = await tools._get_agent_tenant_id(agent_id)
        return await tools._run_with_temp_workspace(
            agent_id,
            tenant_id,
            lambda temp_ws: tools._generate_image(agent_id, temp_ws, arguments, provider),
            sync_back=True,
        )


_register_generate_image("generate_image_siliconflow", "siliconflow")
_register_generate_image("generate_image_openai", "openai")
_register_generate_image("generate_image_google", "google")
_register_generate_image("generate_image_grok", "grok")
_register_generate_image("generate_image_custom", "custom")


@register("discover_resources")
async def discover_resources(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _mcp_tools()._discover_resources(agent_id, arguments)


@register("import_mcp_server")
async def import_mcp_server(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _mcp_tools()._import_mcp_server(agent_id, arguments)


def _register_email(name: str) -> None:
    @register(name)
    async def _handle(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        del user_id, session_id, on_output
        tools = _agent_tools()
        return await tools._handle_email_tool(name, agent_id, tools._agent_workspace_root(agent_id), arguments)


_register_email("send_email")
_register_email("read_emails")
_register_email("reply_email")


@register("publish_page")
async def publish_page(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    tools = _agent_tools()
    return await tools._publish_page(agent_id, user_id, tools._agent_workspace_root(agent_id), arguments)


@register("list_published_pages")
async def list_published_pages(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del arguments, user_id, session_id, on_output
    return await _agent_tools()._list_published_pages(agent_id)


@register("search_clawhub")
async def search_clawhub(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _agent_tools()._search_clawhub(agent_id, arguments)


@register("install_skill")
async def install_skill(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    tools = _agent_tools()
    return await tools._install_skill(agent_id, tools._agent_workspace_root(agent_id), arguments)


@register("get_okr")
async def get_okr(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _okr_read()._get_okr(agent_id, arguments)


@register("get_my_okr")
async def get_my_okr(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _okr_read()._get_my_okr(agent_id, arguments)


@register("update_kr_content")
async def update_kr_content(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    return await _okr_write()._update_kr_content(agent_id, user_id, arguments)


@register("update_kr_progress")
async def update_kr_progress(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    return await _okr_write()._update_kr_progress(agent_id, user_id, arguments)


@register("collect_okr_progress")
async def collect_okr_progress(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del arguments, user_id, session_id, on_output
    return await _okr_reports()._collect_okr_progress(agent_id)


@register("generate_okr_report")
async def generate_okr_report(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _okr_reports()._generate_okr_report(agent_id, arguments)


@register("get_okr_settings")
async def get_okr_settings(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del arguments, user_id, session_id, on_output
    return await _okr_read()._get_okr_settings_tool(agent_id)


@register("create_objective")
async def create_objective(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    return await _okr_write()._create_objective(agent_id, user_id, arguments)


@register("create_key_result")
async def create_key_result(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    return await _okr_write()._create_key_result(agent_id, user_id, arguments)


@register("update_objective")
async def update_objective(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    return await _okr_write()._update_objective(agent_id, user_id, arguments)


@register("update_any_kr_progress")
async def update_any_kr_progress(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del session_id, on_output
    return await _okr_write()._update_any_kr_progress(agent_id, user_id, arguments)


@register("generate_monthly_okr_report")
async def generate_monthly_okr_report(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del arguments, user_id, session_id, on_output
    return await _okr_reports()._generate_monthly_okr_report(agent_id)


@register("upsert_member_daily_report")
async def upsert_member_daily_report(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _okr_reports()._upsert_member_daily_report(agent_id, arguments)


@register("vercel_deploy")
async def vercel_deploy(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    tools = _agent_tools()
    return await _deploy()._vercel_deploy(agent_id, tools._agent_workspace_root(agent_id), arguments)


@register("vercel_list_deployments")
async def vercel_list_deployments(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _deploy()._vercel_list_deployments(agent_id, arguments)


@register("vercel_get_deploy_logs")
async def vercel_get_deploy_logs(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _deploy()._vercel_get_deploy_logs(agent_id, arguments)


@register("vercel_set_env")
async def vercel_set_env(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _deploy_ops()._vercel_set_env(agent_id, arguments)


@register("vercel_manage_domain")
async def vercel_manage_domain(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _deploy_ops()._vercel_manage_domain(agent_id, arguments)


@register("neon_create_database")
async def neon_create_database(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    return await _deploy_ops()._neon_create_database(agent_id, arguments)
