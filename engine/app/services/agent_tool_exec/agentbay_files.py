from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from app.core.logging import logger

from .registry import ToolArguments


def _string_argument(arguments: ToolArguments, name: str, *, remove: bool = False) -> str:
    value = arguments.pop(name, "") if remove else arguments.get(name, "")
    return value if isinstance(value, str) else ""


async def _agentbay_file_transfer(agent_id: uuid.UUID | None, ws: Path, arguments: ToolArguments) -> str:
    """Transfer a file between workspace and an AgentBay environment, or between two environments."""
    if not agent_id:
        return "AgentBay tools require agent context"
    from app.services.agentbay_client import get_agentbay_client_for_agent

    from_type, from_path = _string_argument(arguments, "from_type"), _string_argument(arguments, "from_path")
    to_type, to_path = _string_argument(arguments, "to_type"), _string_argument(arguments, "to_path")
    session_id = _string_argument(arguments, "_session_id", remove=True)
    if not all([from_type, from_path, to_type, to_path]):
        return "Missing required parameters: from_type, from_path, to_type, to_path"
    if from_type == "workspace" and to_type == "workspace":
        return "Cannot transfer workspace → workspace. Use write_file or workspace tools instead."
    if from_type == to_type and from_type != "workspace":
        return f"Same environment ({from_type}) transfer: use agentbay_command_exec with 'cp' to copy files within the same environment."
    env_types = {"browser", "computer", "code"}

    def resolve_workspace(rel_path: str) -> tuple[str | None, str]:
        local = (ws / rel_path).resolve()
        if not str(local).startswith(str(ws.resolve())):
            return None, "Permission denied: path must be inside the agent workspace"
        return str(local), ""

    try:
        if from_type == "workspace" and to_type in env_types:
            local_path, err = resolve_workspace(from_path)
            if err:
                return err
            assert local_path is not None  # noqa: S101 - successful containment resolution returns a local path
            import os

            if not os.path.exists(local_path):  # noqa: ASYNC240 - legacy synchronous workspace existence check
                return f"File not found in workspace: {from_path}"
            client = await get_agentbay_client_for_agent(agent_id, to_type, session_id=session_id)
            session = client._session
            assert session is not None  # noqa: S101 - AgentBay client establishes a session before transfer
            result = await asyncio.to_thread(session.file_system.upload_file, local_path, to_path)
            if result.success:
                msg = f"Transferred workspace/{from_path} → [{to_type}]{to_path} ({result.bytes_sent} bytes)"
                desktop_dir = "/home/wuying/桌面"
                if to_type == "computer" and to_path.startswith(desktop_dir):
                    try:
                        await asyncio.to_thread(
                            session.command.exec, f"DISPLAY=:0 gio info '{to_path}' 2>/dev/null || true"
                        )
                    except Exception as exc:
                        logger.debug(f"[AgentBay] Desktop refresh failed after file transfer: {exc}")
                return msg
            return f"Upload failed: {result.error_message}"
        if from_type in env_types and to_type == "workspace":
            local_path, err = resolve_workspace(to_path)
            if err:
                return err
            assert local_path is not None  # noqa: S101 - successful containment resolution returns a local path
            import os

            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            client = await get_agentbay_client_for_agent(agent_id, from_type, session_id=session_id)
            session = client._session
            assert session is not None  # noqa: S101 - AgentBay client establishes a session before transfer
            result = await asyncio.to_thread(session.file_system.download_file, from_path, local_path)
            if result.success:
                return f"Transferred [{from_type}]{from_path} → workspace/{to_path} ({result.bytes_received} bytes). File available in workspace at: {to_path}"
            return f"Download failed: {result.error_message}"
        if from_type in env_types and to_type in env_types:
            import os
            import uuid as _uuid

            tmp_path = f"/tmp/agentbay_transfer_{_uuid.uuid4().hex}"  # noqa: S108 - required legacy transfer bridge
            try:
                src_client = await get_agentbay_client_for_agent(agent_id, from_type, session_id=session_id)
                src_session = src_client._session
                assert src_session is not None  # noqa: S101 - AgentBay client establishes a session before transfer
                dl_result = await asyncio.to_thread(src_session.file_system.download_file, from_path, tmp_path)
                if not dl_result.success:
                    return f"Transfer failed (download from {from_type}): {dl_result.error_message}"
                dst_client = await get_agentbay_client_for_agent(agent_id, to_type, session_id=session_id)
                dst_session = dst_client._session
                assert dst_session is not None  # noqa: S101 - AgentBay client establishes a session before transfer
                ul_result = await asyncio.to_thread(dst_session.file_system.upload_file, tmp_path, to_path)
                if not ul_result.success:
                    return f"Transfer failed (upload to {to_type}): {ul_result.error_message}"
                return f"Transferred [{from_type}]{from_path} → [{to_type}]{to_path} ({dl_result.bytes_received} bytes)"
            finally:
                try:
                    if os.path.exists(tmp_path):  # noqa: ASYNC240 - legacy temporary bridge cleanup
                        os.remove(tmp_path)
                except Exception as exc:
                    logger.debug(f"[AgentBay] Temporary transfer bridge cleanup failed: {exc}")
        return f"Unsupported transfer: {from_type} → {to_type}"
    except RuntimeError as e:
        return f"{e!s}"
    except Exception as e:
        logger.exception(f"[AgentBay] File transfer failed for agent {agent_id}")
        return f"File transfer failed: {str(e)[:200]}"
