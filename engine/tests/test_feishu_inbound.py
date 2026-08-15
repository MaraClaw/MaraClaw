from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import feishu as feishu_api


AGENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _BodyRequest:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def body(self) -> bytes:
        return self._payload


@pytest.mark.asyncio
async def test_webhook_echoes_challenge() -> None:
    request = _BodyRequest(b'{"challenge": "verify-me"}')

    result = await feishu_api.feishu_event_webhook(AGENT_ID, request)

    assert result == {"challenge": "verify-me"}


@pytest.mark.asyncio
async def test_process_feishu_event_skips_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=True))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 0, "msg": "already processed"}


@pytest.mark.asyncio
async def test_process_feishu_event_missing_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-2", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 1, "msg": "Channel not found"}


@pytest.mark.asyncio
async def test_process_text_group_message_uses_chat_and_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(app_id="cli_app", app_secret="secret")
    logs: list[str] = []
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(feishu_api.channel_inbound, "load_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_api.logger, "info", lambda msg, *args, **kwargs: logs.append(str(msg)))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {
            "header": {"event_id": "evt-3", "event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_type": "text",
                    "chat_type": "group",
                    "chat_id": "oc_group",
                    "content": '{"text":"hi"}',
                },
                "sender": {"sender_id": {"open_id": "ou_open", "user_id": "ou_tenant_user"}},
            },
        },
    )

    assert result == {"code": 1, "msg": "Agent not found"}
    assert any("chat_type=group" in line and "user_id_from_event='ou_tenant_user'" in line for line in logs)
