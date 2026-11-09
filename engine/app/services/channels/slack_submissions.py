import json
import re

from app.core.json_types import JsonObject, json_as_str_or, json_object_from
from app.core.outbound_transport import public_http_client
from app.records.channel_config import ChannelConfigRecord
from app.services.storage import store_agent_upload

from .media_limits import MediaBudget, MediaTooLargeError, read_capped, too_large_note
from .submissions import ChannelEvent, accept_channel_event, is_channel_event_replay


async def accept_slack_event(config: ChannelConfigRecord, body: JsonObject) -> bool:
    event = json_object_from(body.get("event"))
    native_id = json_as_str_or(event.get("ts")) or json_as_str_or(body.get("event_id"))
    if not native_id:
        return False
    channel_id = json_as_str_or(event.get("channel"))
    sender_id = json_as_str_or(event.get("user"))
    files_raw = event.get("files")
    files = [json_object_from(item) for item in files_raw] if isinstance(files_raw, list) else []
    text = re.sub(r"^<@[A-Z0-9]+>\s*", "", json_as_str_or(event.get("text"))).strip()
    native = ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="slack",
        event_key=json.dumps([body.get("team_id"), channel_id, native_id], separators=(",", ":")),
        sender_id=sender_id,
        conversation_id=f"slack_{channel_id}" if channel_id else f"slack_dm_{sender_id}",
        text=text,
        kind="files" if files else "text",
        media_ids=tuple(json_as_str_or(file.get("id")) for file in files),
        is_group=bool(channel_id) and not channel_id.startswith("D"),
    )
    if await is_channel_event_replay(native):
        return True
    enriched = text
    budget = MediaBudget()
    for file in files:
        url = json_as_str_or(file.get("url_private_download")) or json_as_str_or(file.get("url_private"))
        filename = json_as_str_or(file.get("name")) or f"slack_{json_as_str_or(file.get('id'))}.bin"
        try:
            limit = budget.limit_for(filename)
            async with (
                public_http_client(timeout=30, follow_redirects=True, origin="https://files.slack.com") as client,
                client.stream("GET", url, headers={"Authorization": f"Bearer {config.app_secret or ''}"}) as response,
            ):
                data = await read_capped(response, limit)
                content_type = str(response.headers.get("content-type") or "")
        except MediaTooLargeError as error:
            budget.spend(error.read)
            enriched = f"{enriched}\n{too_large_note(filename, error.limit)}".strip()
            continue
        budget.spend(len(data))
        _, workspace_path, _ = await store_agent_upload(config.agent_id, filename, data, content_type=content_type)
        enriched = f"{enriched}\n[file:{workspace_path}]".strip()
    await accept_channel_event(native, content=enriched, saved_content=enriched)
    return True
