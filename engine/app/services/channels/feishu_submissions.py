from __future__ import annotations

import base64
import json
import re

from app.core.json_types import JsonObject, JsonValue, json_as_str_or, json_loads_object, json_object_from
from app.records.channel_config import ChannelConfigRecord
from app.services.feishu_service import feishu_service
from app.services.storage import store_agent_upload

from .submissions import ChannelEvent, accept_channel_event, is_channel_event_replay


def _post_parts(value: JsonValue) -> tuple[list[str], list[str]]:
    text: list[str] = []
    images: list[str] = []
    if isinstance(value, list):
        for item in value:
            item_text, item_images = _post_parts(item)
            text.extend(item_text)
            images.extend(item_images)
    elif isinstance(value, dict):
        tag = value.get("tag")
        if tag in ("text", "a"):
            text.append(json_as_str_or(value.get("text")))
            if tag == "a" and bool(value.get("href")):
                text.append(json_as_str_or(value.get("href")))
        elif tag == "img":
            images.append(json_as_str_or(value.get("image_key")))
        else:
            for item in value.values():
                if isinstance(item, (list, dict)):
                    item_text, item_images = _post_parts(item)
                    text.extend(item_text)
                    images.extend(item_images)
    return text, images


async def accept_feishu_event(config: ChannelConfigRecord, event: JsonObject) -> bool:
    message = json_object_from(event.get("message"))
    message_id = json_as_str_or(message.get("message_id"))
    kind = json_as_str_or(message.get("message_type"), "text")
    if not message_id or kind not in ("text", "post", "image", "file"):
        return False
    sender = json_object_from(json_object_from(event.get("sender")).get("sender_id"))
    external_id = json_as_str_or(sender.get("user_id"))
    open_id = json_as_str_or(sender.get("open_id"))
    chat_id = json_as_str_or(message.get("chat_id"))
    group = message.get("chat_type") == "group" and bool(chat_id)
    body = json_loads_object(json_as_str_or(message.get("content"), "{}"))
    text = re.sub(r"@_user_\d+", "", json_as_str_or(body.get("text"))).strip()
    media: list[str] = []
    if kind == "post":
        parts, media = _post_parts(body)
        text = re.sub(r"@_user_\d+", "", "\n".join(parts)).strip()
    elif kind in ("image", "file"):
        media = [json_as_str_or(body.get("image_key" if kind == "image" else "file_key"))]
    native = ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="feishu",
        event_key=json.dumps([config.app_id, chat_id, message_id], separators=(",", ":")),
        sender_id=external_id or open_id,
        sender_external_id=external_id,
        sender_open_id=open_id,
        conversation_id=f"feishu_group_{chat_id}" if group else f"feishu_p2p_{external_id or open_id}",
        text=text,
        kind=kind,
        media_ids=tuple(media),
        is_group=group,
    )
    if await is_channel_event_replay(native):
        return True
    enriched = text
    saved = text
    for media_id in media:
        resource_type = "file" if kind == "file" else "image"
        data = await feishu_service.download_message_resource(
            config.app_id or "", config.app_secret or "", message_id, media_id, resource_type
        )
        filename = json_as_str_or(body.get("file_name")) or f"image_{media_id[-8:]}.jpg"
        _, workspace_path, _ = await store_agent_upload(
            config.agent_id, filename, data, content_type="image/jpeg" if resource_type == "image" else None
        )
        marker = f"[file:{workspace_path}]"
        saved = f"{saved}\n{marker}".strip()
        if resource_type == "image":
            marker += f"\n[image_data:data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}]"
        enriched = f"{enriched}\n{marker}".strip()
    await accept_channel_event(native, content=enriched, saved_content=saved)
    return True
