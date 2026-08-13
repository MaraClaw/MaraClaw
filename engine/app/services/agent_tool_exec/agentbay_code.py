from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.core.logging import logger

from .agentbay_response import _agentbay_response_text
from .registry import ToolArguments


def _string_argument(arguments: ToolArguments, name: str, default: str = "", *, remove: bool = False) -> str:
    value = arguments.pop(name, default) if remove else arguments.get(name, default)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name, default)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _agentbay_code_execute(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Execute code in the AgentBay code environment."""
    if not agent_id:
        return "❌ AgentBay tools require an agent context."

    from app.services.agentbay_client import get_agentbay_client_for_agent

    language = _string_argument(arguments, "language", "python")
    code = _string_argument(arguments, "code")
    timeout = _integer_argument(arguments, "timeout", 30)

    if not code.strip():
        return "❌ Provide code to execute."

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await client.code_execute(language, code, timeout)

        parts = [f"✅ Code execution complete ({language})"]
        if result.get("stdout"):
            parts.append(f"📤 Output:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"⚠️ Error output:\n{result['stderr']}")
        if result.get("exit_code") != 0:
            parts.append(f"Exit code: {result['exit_code']}")

        return "\n\n".join(parts)
    except RuntimeError as e:
        return f"❌ {e!s}. Configure the AgentBay channel in Agent settings first."
    except Exception as e:
        logger.exception(f"[AgentBay] Code execution failed for agent {agent_id}")
        return f"❌ Code execution failed: {str(e)[:200]}"


async def _agentbay_code_write_file(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Write a text file in the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = _string_argument(arguments, "remote_path") or _string_argument(arguments, "path")
    content = arguments.get("content")
    mode = _string_argument(arguments, "mode", "overwrite")

    if not remote_path.strip():
        return "Missing required argument 'remote_path'"
    if content is None:
        return "Missing required argument 'content'"
    if mode not in ("overwrite", "append"):
        return "Invalid mode. Use 'overwrite' or 'append'."

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        session = client._session
        assert session is not None  # noqa: S101 - AgentBay client establishes a session before filesystem use
        result = await asyncio.to_thread(session.file_system.write_file, remote_path, str(content), mode)
        if result.success:
            byte_count = len(str(content).encode("utf-8"))
            return f"File written in AgentBay Code Sandbox: {remote_path} ({byte_count} bytes, mode={mode})"
        return f"Write failed: {result.error_message}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code write file failed for agent {agent_id}")
        return f"Write file failed: {str(e)[:200]}"


async def _agentbay_code_read_file(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Read a text file from the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = _string_argument(arguments, "remote_path") or _string_argument(arguments, "path")
    if not remote_path.strip():
        return "Missing required argument 'remote_path'"

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        session = client._session
        assert session is not None  # noqa: S101 - AgentBay client establishes a session before filesystem use
        result = await asyncio.to_thread(session.file_system.read_file, remote_path)
        if result.success:
            content = getattr(result, "content", "") or ""
            return f"File read from AgentBay Code Sandbox: {remote_path}\n\n{content[:12000]}"
        return f"Read failed: {result.error_message}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code read file failed for agent {agent_id}")
        return f"Read file failed: {str(e)[:200]}"


async def _agentbay_code_edit_file(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Edit a text file in the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = _string_argument(arguments, "remote_path") or _string_argument(arguments, "path")
    edits = arguments.get("edits")
    dry_run = bool(arguments.get("dry_run", False))

    if not remote_path.strip():
        return "Missing required argument 'remote_path'"
    if not isinstance(edits, list) or not edits:
        return "Missing required argument 'edits'"

    normalized_edits = []
    for edit in edits:
        if not isinstance(edit, dict):
            return "Each edit must be an object with oldText and newText."
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if old_text is None or new_text is None:
            return "Each edit must include oldText and newText."
        normalized_edits.append({"oldText": str(old_text), "newText": str(new_text)})

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        session = client._session
        assert session is not None  # noqa: S101 - AgentBay client establishes a session before filesystem use
        result = await asyncio.to_thread(session.file_system.edit_file, remote_path, normalized_edits, dry_run)
        if result.success:
            action = "Previewed edits for" if dry_run else "Edited"
            return f"{action} AgentBay Code Sandbox file: {remote_path} ({len(normalized_edits)} replacement(s))"
        return f"Edit failed: {result.error_message}"
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code edit file failed for agent {agent_id}")
        return f"Edit file failed: {str(e)[:200]}"


async def _agentbay_command_exec(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Execute a shell command in the AgentBay environment."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    command = _string_argument(arguments, "command")
    timeout_ms = _integer_argument(arguments, "timeout_ms", 50000)
    cwd = _string_argument(arguments, "cwd")
    if not command.strip():
        return "Missing required argument 'command'"

    try:
        _session_id = _string_argument(arguments, "_session_id", remove=True)
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await client.command_exec(command, timeout_ms=timeout_ms, cwd=cwd)
        parts = []
        if result.get("success"):
            parts.append(f"Command executed successfully (exit code: {result.get('exit_code', 0)})")
        else:
            parts.append(f"Command failed (exit code: {result.get('exit_code', -1)})")
        stdout = _agentbay_response_text(result.get("stdout"), "")
        if stdout:
            parts.append(f"stdout:\n{stdout[:3000]}")
        stderr = _agentbay_response_text(result.get("stderr"), "")
        if stderr:
            parts.append(f"stderr:\n{stderr[:1000]}")
        if result.get("error_message"):
            parts.append(f"Error: {result['error_message']}")
        return "\n\n".join(parts)
    except RuntimeError as e:
        return f"{e!s}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Command exec failed for agent {agent_id}")
        return f"Command execution failed: {str(e)[:200]}"
