from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Never

import pytest

from app.api import feishu
from app.core.json_types import JsonObject
from app.services import wechat_channel, wechat_message_processor


def _channel_config(extra_config: JsonObject) -> SimpleNamespace:
    return SimpleNamespace(agent_id=uuid.uuid4(), channel_type="wechat", extra_config=extra_config)


def _valid_message() -> JsonObject:
    return {
        "from_user_id": "wechat-user",
        "context_token": "context-token",
        "item_list": [{"type": 1, "text_item": {"text": "message"}}],
    }


def test_parse_wechat_message_rejects_scalar_and_list_payloads() -> None:
    assert wechat_message_processor._parse_wechat_message("message") is None
    assert wechat_message_processor._parse_wechat_message([]) is None


def test_parse_wechat_message_skips_malformed_items_and_preserves_text_order() -> None:
    parsed = wechat_message_processor._parse_wechat_message(
        {
            "from_user_id": "wechat-user",
            "context_token": "context-token",
            "item_list": [
                {"type": 1, "text_item": {"text": "first"}},
                {"type": True, "text_item": {"text": "ignored"}},
                {"type": 2},
                {"type": 1, "text_item": {"text": "second"}},
                {"type": 1, "text_item": {"text": 1}},
                "invalid",
            ],
        }
    )

    assert parsed is not None
    assert wechat_channel._extract_wechat_text(parsed["item_list"]) == "first\nsecond"


def test_parse_wechat_message_falls_back_to_sender_for_invalid_session() -> None:
    parsed = wechat_message_processor._parse_wechat_message(
        {
            "from_user_id": "wechat-user",
            "session_id": ["invalid"],
            "context_token": "context-token",
            "item_list": [],
        }
    )

    assert parsed is not None
    assert parsed["session_id"] == "wechat-user"


def test_parse_wechat_delivery_config_defaults_malformed_optional_values() -> None:
    parsed = wechat_message_processor._parse_wechat_delivery_config(
        _channel_config({"bot_token": "token", "baseurl": False, "route_tag": ["invalid"]}),
        wechat_channel.WECHAT_ILINK_BASE_URL,
    )

    assert parsed == {
        "token": "token",
        "base_url": wechat_channel.WECHAT_ILINK_BASE_URL,
        "route_tag": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_message", "config"),
    [
        (_valid_message() | {"from_user_id": 1}, _channel_config({"bot_token": "token"})),
        (_valid_message() | {"context_token": []}, _channel_config({"bot_token": "token"})),
        (_valid_message() | {"item_list": "invalid"}, _channel_config({"bot_token": "token"})),
        (_valid_message(), _channel_config({"bot_token": False})),
    ],
)
async def test_process_wechat_message_returns_before_database_for_malformed_boundary_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_message: JsonObject,
    config: SimpleNamespace,
) -> None:
    llm_calls: list[None] = []

    async def unexpected_agent_get(_agent_id: uuid.UUID):
        raise AssertionError("database access")

    async def unexpected_llm_call(*_args: Never, **_kwargs: Never) -> str:
        llm_calls.append(None)
        raise AssertionError("LLM access")

    monkeypatch.setattr(wechat_message_processor.agent_dao, "get", unexpected_agent_get)
    monkeypatch.setattr(feishu, "_call_llm_with_config", unexpected_llm_call)
    monkeypatch.setattr("app.services.channels.inbound.generate_channel_reply", unexpected_llm_call)

    await wechat_message_processor.process_wechat_message(uuid.uuid4(), raw_message, config)

    assert not llm_calls
