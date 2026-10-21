"""Typed inbound message processing for the WeChat iLink connector."""

from __future__ import annotations

import uuid
from typing import Any, NotRequired, TypedDict

from app.core.json_types import JsonValue
from app.core.logging import logger
from app.records.channel_config import ChannelConfigRecord


class WeChatTextItem(TypedDict):
    text: str


class WeChatMessageItem(TypedDict):
    type: int
    text_item: NotRequired[WeChatTextItem]


class WeChatInboundMessage(TypedDict):
    message_id: str
    from_user_id: str
    session_id: str
    context_token: str | None
    item_list: list[WeChatMessageItem]


class WeChatDeliveryConfig(TypedDict):
    token: str
    base_url: str
    route_tag: str | None


def _nonempty_string(value: JsonValue | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _parse_wechat_message_item(value: JsonValue) -> WeChatMessageItem | None:
    if not isinstance(value, dict):
        return None
    item_type = value.get("type")
    if not isinstance(item_type, int) or isinstance(item_type, bool):
        return None

    item: WeChatMessageItem = {"type": item_type}
    text_item = value.get("text_item")
    if not isinstance(text_item, dict):
        return item
    text = text_item.get("text")
    if not isinstance(text, str):
        return item
    item["text_item"] = {"text": text}
    return item


def _parse_wechat_message(raw_message: JsonValue) -> WeChatInboundMessage | None:
    if not isinstance(raw_message, dict):
        return None
    from_user_id = _nonempty_string(raw_message.get("from_user_id"))
    if from_user_id is None:
        return None
    raw_items = raw_message.get("item_list")
    if not isinstance(raw_items, list):
        return None
    return {
        "message_id": str(raw_message.get("message_id") or ""),
        "from_user_id": from_user_id,
        "session_id": _nonempty_string(raw_message.get("session_id")) or from_user_id,
        "context_token": _nonempty_string(raw_message.get("context_token")),
        "item_list": [item for raw_item in raw_items if (item := _parse_wechat_message_item(raw_item)) is not None],
    }


def _parse_wechat_delivery_config(
    config: ChannelConfigRecord | object, fallback_base_url: str
) -> WeChatDeliveryConfig | None:
    extra_raw = getattr(config, "extra_config", None) or {}
    extra: dict[str, Any] = dict[str, Any](extra_raw) if isinstance(extra_raw, dict) else {}
    token = _nonempty_string(extra.get("bot_token"))
    if token is None:
        return None
    return {
        "token": token,
        "base_url": _nonempty_string(extra.get("baseurl")) or fallback_base_url,
        "route_tag": _nonempty_string(extra.get("route_tag")),
    }


async def process_wechat_message(agent_id: uuid.UUID, raw_message: JsonValue, config: ChannelConfigRecord) -> None:
    from app.services.channels.submissions import ChannelEvent, accept_channel_event, is_channel_event_replay
    from app.services.wechat_channel import (
        WECHAT_ILINK_BASE_URL,
        _extract_wechat_text,
        remember_wechat_context,
    )

    message = _parse_wechat_message(raw_message)
    if message is None:
        return
    from_user_id = message["from_user_id"]
    if from_user_id == (getattr(config, "app_id", None) or "").strip():
        return
    user_text = _extract_wechat_text(message["item_list"])
    if not user_text:
        return
    context_token = message["context_token"]
    if context_token is None:
        logger.warning(f"[WeChat] Missing context_token for agent {agent_id}, message skipped")
        return
    delivery_config = _parse_wechat_delivery_config(config, WECHAT_ILINK_BASE_URL)
    if delivery_config is None:
        return

    if not message["message_id"]:
        raise ValueError("WeChat message ID is required")
    native = ChannelEvent(
        config_id=config.id,
        agent_id=agent_id,
        provider="wechat",
        event_key=f"{config.app_id}:{message['session_id']}:{message['message_id']}",
        sender_id=from_user_id,
        conversation_id=f"wechat_{message['session_id']}",
        text=user_text,
    )
    if await is_channel_event_replay(native):
        return
    await remember_wechat_context(
        agent_id=agent_id,
        from_user_id=from_user_id,
        context_token=context_token,
        conv_id=native.conversation_id,
    )
    await accept_channel_event(native)
