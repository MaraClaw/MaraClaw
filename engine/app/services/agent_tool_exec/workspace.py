from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.services.agent_tool_exec.registry import ToolArguments
from app.services.agent_tool_exec.workspace_paths import _normalize_tool_rel_path, _tool_storage_key
from app.services.agent_tool_exec.workspace_temp import (
    _is_enterprise_info_path,
    _prepare_temp_workspace,
    flush_temp_workspace,
)
from app.services.focus_service import is_focus_file_path
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.workspace_collaboration import (
    delete_workspace_file,
    move_workspace_path,
    normalize_workspace_path,
    write_workspace_file,
)

type TempWorkspaceRunner = Callable[[Path], Awaitable[str]]


def _string_argument(arguments: ToolArguments, name: str) -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else ""


async def _run_with_temp_workspace(
    agent_id: uuid.UUID,
    tenant_id: str | None,
    runner: TempWorkspaceRunner,
    *,
    paths: list[str] | None = None,
    sync_back: bool = False,
) -> str:
    """Materialize a temporary workspace for tools that require local files."""
    temp_workspace = await _prepare_temp_workspace(agent_id, tenant_id=tenant_id, paths=paths)
    try:
        result = await runner(temp_workspace.root)
        if sync_back:
            flush_result = await flush_temp_workspace(temp_workspace, conflict_mode="fail")
            if flush_result["conflicted"]:
                conflict_list = ", ".join(flush_result["conflicted"][:5])
                return f"❌ Workspace sync conflict for: {conflict_list}"
        return result
    finally:
        temp_workspace.cleanup()


async def _execute_workspace_mutation(
    tool_name: str,
    arguments: ToolArguments,
    *,
    agent_id: uuid.UUID,
    base_dir: Path,
    session_id: str | None,
) -> str:
    """Handle shared workspace mutations for both direct and normal tool execution."""

    if tool_name == "write_file":
        path = _string_argument(arguments, "path")
        content = _string_argument(arguments, "content")
        if not path:
            return "❌ Missing required argument 'path' for write_file. Please provide a file path like 'skills/my-skill/SKILL.md'"
        if "content" not in arguments:
            return "❌ Missing required argument 'content' for write_file"
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use upsert_focus_item or complete_focus_item."
        if _is_enterprise_info_path(path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        write_result = await write_workspace_file(
            None,
            agent_id=agent_id,
            base_dir=base_dir,
            path=path,
            content=content,
            actor_type="agent",
            actor_id=agent_id,
            operation="write",
            session_id=session_id,
            enforce_human_lock=True,
        )
        return (
            f"✅ Written to {write_result.path} ({len(content)} chars)"
            if write_result.ok
            else f"❌ {write_result.message}"
        )

    if tool_name == "move_file":
        source_path = _string_argument(arguments, "source_path")
        destination_path = _string_argument(arguments, "destination_path")
        if not source_path:
            return "❌ Missing required argument 'source_path' for move_file"
        if not destination_path:
            return "❌ Missing required argument 'destination_path' for move_file"
        if is_focus_file_path(source_path) or is_focus_file_path(destination_path):
            return "❌ Focus is no longer stored in focus.md. Use Focus tools instead."
        if str(source_path).strip("/") in {"tasks.json", "soul.md"}:
            return f"❌ {source_path} cannot be moved (protected)"
        if _is_enterprise_info_path(source_path) or _is_enterprise_info_path(destination_path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        move_result = await move_workspace_path(
            None,
            agent_id=agent_id,
            base_dir=base_dir,
            source_path=source_path,
            destination_path=destination_path,
            actor_type="agent",
            actor_id=agent_id,
            session_id=session_id,
            enforce_human_lock=True,
            overwrite=arguments.get("overwrite") is True,
        )
        return f"✅ {move_result.message}" if move_result.ok else f"❌ {move_result.message}"

    if tool_name == "delete_file":
        path = _string_argument(arguments, "path")
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use Focus tools instead."
        if _is_enterprise_info_path(path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        delete_result = await delete_workspace_file(
            None,
            agent_id=agent_id,
            base_dir=base_dir,
            path=path,
            actor_type="agent",
            actor_id=agent_id,
            session_id=session_id,
            enforce_human_lock=True,
        )
        return f"✅ Deleted {delete_result.path}" if delete_result.ok else f"❌ {delete_result.message}"

    if tool_name == "edit_file":
        path = _string_argument(arguments, "path")
        old_string = _string_argument(arguments, "old_string")
        new_string = _string_argument(arguments, "new_string")
        if not path:
            return "❌ Missing required argument 'path' for edit_file"
        if "old_string" not in arguments:
            return "❌ Missing required argument 'old_string' for edit_file"
        if "new_string" not in arguments:
            return "❌ Missing required argument 'new_string' for edit_file"
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use upsert_focus_item or complete_focus_item."
        if _is_enterprise_info_path(path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )

        replace_all = arguments.get("replace_all") is True
        storage = get_storage_backend()
        storage_key, normalized_path, _ = _tool_storage_key(
            agent_id,
            path,
            None,
            normalize_workspace_path_fn=normalize_workspace_path,
            normalize_tool_rel_path=_normalize_tool_rel_path,
            is_enterprise_info_path=_is_enterprise_info_path,
            normalize_storage_key_fn=normalize_storage_key,
        )
        if not await storage.is_file(storage_key):
            return f"File not found: {path}"

        content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        if old_string not in content:
            return (
                f"❌ 'old_string' not found in {path}. Please check the exact text including whitespace and newlines."
            )
        count = content.count(old_string)
        if count > 1 and not replace_all:
            return f"❌ 'old_string' appears {count} times in {path}. Use replace_all=true or provide more context to make the match unique."

        new_content = (
            content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        )
        write_result = await write_workspace_file(
            None,
            agent_id=agent_id,
            base_dir=base_dir,
            path=normalized_path,
            content=new_content,
            actor_type="agent",
            actor_id=agent_id,
            operation="edit",
            session_id=session_id,
            enforce_human_lock=True,
        )
        replaced = count if replace_all else 1
        return (
            f"✅ Replaced {replaced} occurrence(s) in {write_result.path}"
            if write_result.ok
            else f"❌ {write_result.message}"
        )

    return f"Tool {tool_name} does not support workspace mutation execution"
