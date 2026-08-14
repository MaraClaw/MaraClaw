from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from pathlib import Path

from anyio import to_thread
from httpx import AsyncClient, Response

from app.config import get_settings
from app.core.json_types import (
    JsonObject,
    json_as_str,
    json_as_str_or,
    json_object_from,
    json_object_from_response,
    object_list_from_row,
)
from app.records.channel_config import ChannelConfigRecord
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments

from . import channel_context


def _httpx_client(*, timeout: float = 5.0, follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


def _response_mapping(response: Response) -> JsonObject:
    return json_object_from_response(response)


async def _resolve_path(path: Path) -> Path:
    return await to_thread.run_sync(path.resolve)


async def _run_path_call[T](call: Callable[[], T]) -> T:
    return await to_thread.run_sync(call)


async def _send_channel_file(agent_id: uuid.UUID, ws: Path, arguments: ToolArguments) -> str:
    """Send a file to an explicit channel recipient, current channel, or web download URL."""
    file_path_value = arguments.get("file_path", "")
    message_value = arguments.get("message", "")
    member_name_value = arguments.get("member_name", "")
    rel_path = file_path_value.strip() if isinstance(file_path_value, str) else ""
    accompany_msg = message_value if isinstance(message_value, str) else ""
    member_name = member_name_value.strip() if isinstance(member_name_value, str) else ""
    if not rel_path:
        return "Error: file_path is required"

    file_path = await _resolve_path(ws / rel_path)
    ws_resolved = await _resolve_path(ws)
    if not str(file_path).startswith(str(ws_resolved)):
        file_path = await _resolve_path(agent_tools.WORKSPACE_ROOT / str(agent_id) / rel_path)
        if not await _run_path_call(file_path.exists):
            return f"Error: File not found: {rel_path}"
    if not await _run_path_call(file_path.exists):
        return f"Error: File not found: {rel_path}"

    if member_name:
        result = await _send_file_to_recipient(agent_id, file_path, member_name, accompany_msg)
        if result:
            return result
        return (
            f"Failed to send file to '{member_name}': recipient not reachable via configured channels. "
            + "Use send_message_to_agent for digital employees, or omit member_name to return a download link."
        )

    sender = channel_context.channel_file_sender.get()
    if sender is not None:
        try:
            await sender(file_path, accompany_msg)
            return f"File '{file_path.name}' sent to user via channel."
        except Exception as error:
            return f"Failed to send file: {error}"

    aid = channel_context.channel_web_agent_id.get() or str(agent_id)
    base_abs = await _resolve_path(agent_tools.WORKSPACE_ROOT / str(agent_id))
    try:
        file_rel = str((await _resolve_path(file_path)).relative_to(base_abs))
    except ValueError:
        file_rel = rel_path
    settings = get_settings()
    base_url = getattr(settings, "BASE_URL", "").rstrip("/") or ""
    download_url = f"{base_url}/api/agents/{aid}/files/download?path={file_rel}"
    msg = f"File ready: [{file_path.name}]({download_url})"
    if accompany_msg:
        msg = accompany_msg + "\n\n" + msg
    return msg


async def _send_file_to_recipient(
    agent_id: uuid.UUID,
    file_path: Path,
    member_name: str,
    message: str = "",
) -> str | None:
    """Resolve a recipient by name and send file via their reachable channel."""
    from app.dao.channel_config_dao import channel_config_dao

    feishu_config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="feishu")
    if feishu_config:
        feishu_result = await _send_file_via_feishu(agent_id, feishu_config, file_path, member_name, message)
        if feishu_result:
            return feishu_result

    slack_config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="slack")
    if slack_config:
        slack_result = await _send_file_via_slack(agent_id, slack_config, file_path, member_name, message)
        if slack_result:
            return slack_result

    return None


async def _resolve_feishu_recipient(
    agent_id: uuid.UUID, config: ChannelConfigRecord, member_name: str
) -> tuple[str, str] | None:
    """Resolve a Feishu recipient by name. Returns (receive_id, id_type) or None."""
    del config
    search_result = await agent_tools._feishu_user_search(agent_id, {"name": member_name})

    uid_match = re.search(r"user_id: `([A-Za-z0-9]+)`", search_result)
    oid_match = re.search(r"open_id: `(ou_[A-Za-z0-9]+)`", search_result)

    if uid_match:
        return (uid_match.group(1), "user_id")
    if oid_match:
        return (oid_match.group(1), "open_id")

    from app.dao.agent_relationship_dao import agent_relationship_dao

    for relationship in await agent_relationship_dao.list_for_agent_with_members(agent_id):
        if relationship.member and relationship.member.name == member_name:
            if relationship.member.external_id:
                return (relationship.member.external_id, "user_id")
            if relationship.member.open_id:
                return (relationship.member.open_id, "open_id")
            break
    return None


async def _send_file_via_feishu(
    agent_id: uuid.UUID, config: ChannelConfigRecord, file_path: Path, member_name: str, message: str
) -> str | None:
    """Send file to a person via Feishu. Returns result string or None."""
    recipient = await _resolve_feishu_recipient(agent_id, config, member_name)
    if not recipient:
        return None

    receive_id, id_type = recipient
    from app.services.feishu_service import feishu_service

    try:
        _ = await feishu_service.upload_and_send_file(
            config.app_id,
            config.app_secret,
            receive_id,
            file_path,
            receive_id_type=id_type,
            accompany_msg=message,
        )
        return f"File '{file_path.name}' sent to {member_name} via Feishu."
    except Exception as error:
        import json as json_module

        settings = get_settings()
        base_url = getattr(settings, "BASE_URL", "").rstrip("/") or ""
        base_abs = await _resolve_path(agent_tools.WORKSPACE_ROOT / str(agent_id))
        try:
            relative_path = str((await _resolve_path(file_path)).relative_to(base_abs))
        except ValueError:
            relative_path = file_path.name
        parts = []
        if message:
            parts.append(message)
        if base_url:
            download_url = f"{base_url}/api/agents/{agent_id}/files/download?path={relative_path}"
            parts.append(f"{file_path.name}\n{download_url}")
        parts.append(
            f"File upload failed ({error}). If you need direct file sending, enable im:resource permission in Feishu."
        )
        try:
            _ = await feishu_service.send_message(
                config.app_id,
                config.app_secret,
                receive_id,
                "text",
                json_module.dumps({"text": "\n\n".join(parts)}, ensure_ascii=False),
                receive_id_type=id_type,
            )
            return f"File upload to Feishu failed, sent download link to {member_name} instead."
        except Exception:
            return f"Failed to send file to {member_name} via Feishu: {error}"


async def _send_file_via_slack(
    agent_id: uuid.UUID, config: ChannelConfigRecord, file_path: Path, member_name: str, message: str
) -> str | None:
    """Send file to a person via Slack DM. Returns result string or None."""
    del agent_id
    bot_token = config.app_secret or ""
    if not bot_token:
        return None

    try:
        async with _httpx_client(timeout=10) as client:
            resp = await client.get(
                "https://slack.com/api/users.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"limit": 200},
            )
            data = _response_mapping(resp)
            if not data.get("ok"):
                return None
            slack_user_id = None
            for user in object_list_from_row(data.get("members")):
                member = json_object_from(user)
                profile = json_object_from(member.get("profile"))
                display = (
                    json_as_str_or(profile.get("display_name"))
                    or json_as_str_or(profile.get("real_name"))
                    or json_as_str_or(member.get("real_name"))
                )
                if display == member_name or json_as_str(member.get("name")) == member_name:
                    slack_user_id = json_as_str(member.get("id"))
                    break
            if not slack_user_id:
                return None

            dm_resp = await client.post(
                "https://slack.com/api/conversations.open",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"users": slack_user_id},
            )
            dm_data = _response_mapping(dm_resp)
            if not dm_data.get("ok"):
                return None
            channel_id = json_as_str(json_object_from(dm_data.get("channel")).get("id"))
            if not channel_id:
                return None

            upload_url_resp = await client.post(
                "https://slack.com/api/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                data={"filename": file_path.name, "length": str((await _run_path_call(file_path.stat)).st_size)},
            )
            upload_data = _response_mapping(upload_url_resp)
            if not upload_data.get("ok"):
                return f"Slack file upload failed: {upload_data.get('error')}"
            upload_url = json_as_str(upload_data.get("upload_url"))
            file_id = json_as_str(upload_data.get("file_id"))
            if not upload_url or not file_id:
                return f"Slack file upload failed: {upload_data.get('error')}"
            await client.post(
                upload_url,
                content=await _run_path_call(file_path.read_bytes),
                headers={"Content-Type": "application/octet-stream"},
            )
            complete = await client.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={
                    "files": [{"id": file_id}],
                    "channel_id": channel_id,
                    "initial_comment": message or "",
                },
            )
            complete_data = _response_mapping(complete)
            if not complete_data.get("ok"):
                return f"Slack file upload complete failed: {complete_data.get('error')}"
            return f"File '{file_path.name}' sent to {member_name} via Slack."
    except Exception as error:
        return f"Failed to send file via Slack: {error}"
