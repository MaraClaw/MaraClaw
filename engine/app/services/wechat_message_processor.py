"""Typed inbound message processing for the WeChat iLink connector."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, NotRequired, TypedDict

from app.core.json_types import JsonValue
from app.core.logging import logger
from app.dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.channel_config import ChannelConfigRecord
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import channel_user_service


class WeChatTextItem(TypedDict):
    text: str


class WeChatMessageItem(TypedDict):
    type: int
    text_item: NotRequired[WeChatTextItem]


class WeChatInboundMessage(TypedDict):
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
    token = _nonempty_string(extra.get("bot_token") if isinstance(extra, dict) else None)
    if token is None:
        return None
    return {
        "token": token,
        "base_url": _nonempty_string(extra.get("baseurl") if isinstance(extra, dict) else None) or fallback_base_url,
        "route_tag": _nonempty_string(extra.get("route_tag") if isinstance(extra, dict) else None),
    }


async def process_wechat_message(
    agent_id: uuid.UUID, raw_message: JsonValue, config: ChannelConfigRecord | object
) -> None:
    from app.api.feishu import _load_agent_and_model
    from app.services.activity_logger import log_activity
    from app.services.llm.utils import convert_chat_messages_to_llm_format
    from app.services.wechat_channel import (
        WECHAT_ILINK_BASE_URL,
        _extract_wechat_text,
        remember_wechat_context,
        send_wechat_text_message,
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

    agent_obj = await agent_dao.get(agent_id)
    if not agent_obj:
        return

    extra_info = {
        "name": f"WeChat User {from_user_id[:8]}",
        "external_id": from_user_id,
    }
    platform_user = await channel_user_service.resolve_channel_user(
        db=None,
        agent=agent_obj,
        channel_type="wechat",
        external_user_id=from_user_id,
        extra_info=extra_info,
    )
    platform_user_id = platform_user.id
    conv_id = f"wechat_{message['session_id']}"

    sess = await find_or_create_channel_session(
        db=None,
        agent_id=agent_id,
        user_id=platform_user_id,
        external_conv_id=conv_id,
        source_channel="wechat",
        first_message_title=user_text,
    )
    session_conv_id = str(sess.id)
    await remember_wechat_context(
        None,
        agent_id=agent_id,
        from_user_id=from_user_id,
        context_token=context_token,
        conv_id=conv_id,
    )

    from app.services.channels.inbound import routing_history_limit

    history_msgs = await chat_message_dao.list_recent(
        agent_id=agent_id,
        conversation_id=session_conv_id,
        limit=routing_history_limit(agent_obj.context_window_size),
    )
    history = convert_chat_messages_to_llm_format(history_msgs)

    _ = await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        role="user",
        content=user_text,
        conversation_id=session_conv_id,
    )
    _ = await chat_session_dao.update(db_obj=sess, obj_in={"last_message_at": datetime.now(UTC)})
    _agent_model, _llm_model, _fallback_model = await _load_agent_and_model(None, agent_id)

    from app.services.channels import inbound as channel_inbound

    reply_text = await channel_inbound.generate_channel_reply(
        agent_id=agent_id,
        user_text=user_text,
        history=history,
        user_id=platform_user_id,
        session_id=session_conv_id,
        agent_model=_agent_model,
        llm_model=_llm_model,
        fallback_model=_fallback_model,
    )
    if channel_inbound.is_queued_channel_reply(reply_text):
        return
    await send_wechat_text_message(
        token=delivery_config["token"],
        base_url=delivery_config["base_url"],
        to_user_id=from_user_id,
        context_token=context_token,
        text=reply_text,
        route_tag=delivery_config["route_tag"],
    )

    _ = await chat_message_dao.insert_message(
        agent_id=agent_id,
        user_id=platform_user_id,
        role="assistant",
        content=reply_text,
        conversation_id=session_conv_id,
    )
    try:
        fresh_session = await chat_session_dao.get(uuid.UUID(session_conv_id))
        if fresh_session:
            _ = await chat_session_dao.update(db_obj=fresh_session, obj_in={"last_message_at": datetime.now(UTC)})
    except ValueError, TypeError:
        pass

    await log_activity(
        agent_id,
        "chat_reply",
        f"Replied to WeChat message: {reply_text[:80]}",
        detail={"channel": "wechat", "user_text": user_text[:200], "reply": reply_text[:500]},
    )
