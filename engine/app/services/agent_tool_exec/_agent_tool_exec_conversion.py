from __future__ import annotations

import importlib
import uuid

from . import workspace
from .registry import ToolArguments, ToolOutputCallback, current_execution_context, register

document_convert = importlib.import_module("app.services.agent_tool_exec.document_convert")


def _path_argument(arguments: ToolArguments, name: str) -> str | None:
    value = arguments.get(name)
    return value if isinstance(value, str) else None


@register("convert_csv_to_xlsx")
async def convert_csv_to_xlsx(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: document_convert._convert_csv_to_xlsx(agent_id, temp_ws, arguments),
        paths=agent_tools._non_empty_paths(
            _path_argument(arguments, "source_path"), _path_argument(arguments, "target_path")
        ),
        sync_back=True,
    )


@register("convert_html_to_pdf")
async def convert_html_to_pdf(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: document_convert._convert_html_to_pdf(agent_id, temp_ws, arguments),
        paths=agent_tools._non_empty_paths(
            _path_argument(arguments, "source_path"), _path_argument(arguments, "target_path")
        ),
        sync_back=True,
    )


@register("convert_html_to_pptx")
async def convert_html_to_pptx(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: document_convert._convert_html_to_pptx(agent_id, temp_ws, arguments),
        paths=agent_tools._non_empty_paths(
            _path_argument(arguments, "source_path"), _path_argument(arguments, "target_path")
        ),
        sync_back=True,
    )


@register("convert_markdown_to_docx")
async def convert_markdown_to_docx(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: document_convert._convert_markdown_to_docx(agent_id, temp_ws, arguments),
        paths=agent_tools._non_empty_paths(
            _path_argument(arguments, "source_path"), _path_argument(arguments, "target_path")
        ),
        sync_back=True,
    )


@register("convert_markdown_to_pdf")
async def convert_markdown_to_pdf(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace._run_with_temp_workspace(
        agent_id,
        tenant_id,
        lambda temp_ws: document_convert._convert_markdown_to_pdf(agent_id, temp_ws, arguments),
        paths=agent_tools._non_empty_paths(
            _path_argument(arguments, "source_path"), _path_argument(arguments, "target_path")
        ),
        sync_back=True,
    )
