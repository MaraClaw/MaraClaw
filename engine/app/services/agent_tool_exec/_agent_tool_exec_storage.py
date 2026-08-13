from __future__ import annotations

import importlib
import uuid

from app.services import focus_service
from app.services.focus_service import is_focus_file_path

from . import workspace, workspace_read
from .registry import ToolArguments, ToolOutputCallback, current_execution_context, register

documents = importlib.import_module("app.services.agent_tool_exec.documents")


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name, default)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


@register("list_files")
async def list_files(
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
    return await workspace_read._storage_list_dir(
        agent_id,
        _string_argument(arguments, "path"),
        tenant_id=tenant_id,
        get_storage_backend=agent_tools.get_storage_backend,
        tool_storage_key=agent_tools._tool_storage_key,
        display_size=agent_tools._display_size,
    )


@register("list_focus_items")
async def list_focus_items(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    items = await focus_service.list_focus_items(
        agent_id,
        include_completed=bool(arguments.get("include_completed", True)),
    )
    if not items:
        return "No Focus items."

    lines = ["Focus items:"]
    for item in items:
        label = "completed" if item["status"] == "completed" else "in_progress"
        kind = f", {item['kind']}" if item.get("kind") == "system" else ""
        if item.get("title"):
            lines.append(f"- {item['title']} ({item['key']}) [{label}{kind}]: {item['description']}")
        else:
            lines.append(f"- {item['key']} [{label}{kind}]: {item['description']}")
    return "\n".join(lines)


@register("upsert_focus_item")
async def upsert_focus_item(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    description = _string_argument(arguments, "description").strip()
    if not description:
        return "❌ Missing required argument 'description' for upsert_focus_item"
    item = await focus_service.upsert_focus_item(
        agent_id,
        key=_string_argument(arguments, "key") or None,
        title=_string_argument(arguments, "title") or None,
        description=description,
        status="in_progress",
        kind=_string_argument(arguments, "kind", "normal"),
        source=_string_argument(arguments, "source", "user"),
        metadata={"tool": "upsert_focus_item"},
    )
    return (
        f"✅ Focus item saved: {item['key']} (title: {item['title']}) — {item['description']}"
        if item.get("title")
        else f"✅ Focus item saved: {item['key']} — {item['description']}"
    )


@register("complete_focus_item")
async def complete_focus_item(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    key = _string_argument(arguments, "key").strip()
    if not key:
        return "❌ Missing required argument 'key' for complete_focus_item"
    item = await focus_service.complete_focus_item(agent_id, key=key)
    return f"✅ Focus item completed: {key}" if item else f"❌ Focus item not found: {key}"


@register("read_file")
async def read_file(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    path = _string_argument(arguments, "path")
    if not path:
        return "❌ Missing required argument 'path' for read_file"
    if is_focus_file_path(path):
        return "❌ Focus is no longer stored in focus.md. Use list_focus_items, upsert_focus_item, and complete_focus_item."
    offset = _integer_argument(arguments, "offset", 0)
    limit = _integer_argument(arguments, "limit", 2000)
    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await workspace_read._storage_read_file(
        agent_id,
        path,
        tenant_id=tenant_id,
        offset=offset,
        limit=limit,
        get_storage_backend=agent_tools.get_storage_backend,
        tool_storage_key=agent_tools._tool_storage_key,
    )


@register("read_document")
async def read_document(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, session_id, on_output
    from app.services import agent_tools

    path = _string_argument(arguments, "path")
    if not path:
        return "❌ Missing required argument 'path' for read_document"
    max_chars = min(_integer_argument(arguments, "max_chars", 8000), 20000)
    context = current_execution_context()
    tenant_id = context.tenant_id if context is not None else await agent_tools._get_agent_tenant_id(agent_id)
    return await documents._read_document_from_storage(agent_id, path, max_chars=max_chars, tenant_id=tenant_id)


@register("write_file")
async def write_file(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    base_dir = context.workspace_root if context is not None else agent_tools._agent_workspace_root(agent_id)
    return await workspace._execute_workspace_mutation(
        "write_file",
        arguments,
        agent_id=agent_id,
        base_dir=base_dir,
        session_id=session_id,
    )


@register("move_file")
async def move_file(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    base_dir = context.workspace_root if context is not None else agent_tools._agent_workspace_root(agent_id)
    return await workspace._execute_workspace_mutation(
        "move_file",
        arguments,
        agent_id=agent_id,
        base_dir=base_dir,
        session_id=session_id,
    )


@register("delete_file")
async def delete_file(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    base_dir = context.workspace_root if context is not None else agent_tools._agent_workspace_root(agent_id)
    return await workspace._execute_workspace_mutation(
        "delete_file",
        arguments,
        agent_id=agent_id,
        base_dir=base_dir,
        session_id=session_id,
    )


@register("edit_file")
async def edit_file(
    *,
    arguments: ToolArguments,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    on_output: ToolOutputCallback | None,
) -> str:
    del user_id, on_output
    from app.services import agent_tools

    context = current_execution_context()
    base_dir = context.workspace_root if context is not None else agent_tools._agent_workspace_root(agent_id)
    return await workspace._execute_workspace_mutation(
        "edit_file",
        arguments,
        agent_id=agent_id,
        base_dir=base_dir,
        session_id=session_id,
    )
