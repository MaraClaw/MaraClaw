from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from nacl.signing import SigningKey
from starlette.requests import Request

from app.api import discord_bot, feishu, google_chat, slack, teams, whatsapp
from app.core.json_types import JsonObject
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.services.channels import dedup, inbound
from test_channel_shared_processors import context as context, pipeline as pipeline

WEBHOOKS = [
    "slack",
    "slack_upload",
    "slack_file_only",
    "whatsapp",
    "teams",
    "discord",
    "feishu",
    "feishu_image",
    "feishu_file",
    "google_inline",
]
ACK_ONLY = {"slack_file_only", "feishu_file"}


@pytest.fixture
def webhook_pipeline(pipeline, monkeypatch):
    agent, _, _, route, config, *_ = pipeline
    config.is_configured = True
    config.encrypt_key = "signing-secret"
    config.verification_token = "verification-token"
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat.gchat, "audience_for_config", lambda _: "")
    for module in (slack, whatsapp, discord_bot):
        monkeypatch.setattr(module, "find_or_create_channel_session", AsyncMock(return_value=route))
    monkeypatch.setattr(feishu, "_load_agent_and_model", AsyncMock(return_value=(agent, None, None)))
    monkeypatch.setattr(dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(dedup, "mark_processed_shared", AsyncMock())
    monkeypatch.setattr(chat_message_dao, "insert_message", AsyncMock())
    monkeypatch.setattr(inbound, "persist_assistant_message", AsyncMock())
    monkeypatch.setattr(chat_session_dao, "update", AsyncMock())
    monkeypatch.setattr(feishu.feishu_service, "send_message", AsyncMock(return_value={}))
    monkeypatch.setattr(feishu.feishu_service, "download_message_resource", AsyncMock(return_value=b"image-bytes"))
    upload = AsyncMock(return_value=("key", "workspace/uploads/upload.bin", "path"))
    monkeypatch.setattr(slack, "store_agent_upload", upload)
    monkeypatch.setattr(feishu, "store_agent_upload", upload)
    storage = MagicMock()
    storage.exists = AsyncMock(return_value=False)
    monkeypatch.setattr(feishu, "get_storage_backend", lambda: storage)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(200, json={}) if request.method == "POST" else httpx.Response(200, content=b"file-bytes")
        )
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: client_class(transport=transport))
    scheduled = []

    def schedule(coroutine, description):
        scheduled.append(coroutine)

    monkeypatch.setattr(feishu, "schedule_background_task", schedule)
    monkeypatch.setattr("app.api.background_tasks.schedule_background_task", schedule)
    yield pipeline, scheduled
    for coroutine in scheduled:
        coroutine.close()


def request_for(payload, headers=None):
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "headers": [(key.encode(), value.encode()) for key, value in (headers or {}).items()]}, receive
    )


async def invoke_webhook(name, webhook_pipeline, monkeypatch, *, group=False):
    pipeline, scheduled = webhook_pipeline
    agent, _, _, _, config, *_ = pipeline
    match name:
        case "slack" | "slack_upload" | "slack_file_only":
            event: JsonObject = {
                "type": "message",
                "user": "U123",
                "channel": "C123" if group else "D123",
                "text": "" if name == "slack_file_only" else "hello",
            }
            if name != "slack":
                event["files"] = [{"name": "upload.bin", "url_private_download": "https://provider.invalid/file"}]
            payload = {"type": "event_callback", "event": event}
            stamp = str(int(time.time()))
            signature = hmac.new(
                b"signing-secret", b"v0:" + stamp.encode() + b":" + json.dumps(payload).encode(), hashlib.sha256
            ).hexdigest()
            await slack.slack_event_webhook(
                agent.id,
                request_for(payload, {"x-slack-request-timestamp": stamp, "x-slack-signature": f"v0={signature}"}),
            )
        case "whatsapp":
            payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [{"from": "15551234567", "type": "text", "text": {"body": "hello"}}]
                                }
                            }
                        ]
                    }
                ]
            }
            signature = hmac.new(b"signing-secret", json.dumps(payload).encode(), hashlib.sha256).hexdigest()
            await whatsapp.whatsapp_event_webhook(
                agent.id, request_for(payload, {"x-hub-signature-256": f"sha256={signature}"})
            )
        case "teams":
            activity = {
                "type": "message",
                "text": "hello",
                "from": {"id": "teams-user"},
                "conversation": {"id": "teams-conversation", "conversationType": "groupChat" if group else "personal"},
            }
            monkeypatch.setattr(teams, "authenticate_teams_activity", AsyncMock(return_value=activity))
            await teams.teams_event_webhook(agent.id, request_for(activity))
        case "discord":
            key = SigningKey.generate()
            config.encrypt_key = key.verify_key.encode().hex()
            payload = {
                "type": 2,
                "channel_id": "123",
                "user": {"id": "456"},
                "data": {"name": "ask", "options": [{"name": "message", "value": "hello"}]},
            }
            if group:
                payload["guild_id"] = "guild-id"
            stamp = str(int(time.time()))
            signature = key.sign(stamp.encode() + json.dumps(payload).encode()).signature.hex()
            await discord_bot.discord_interaction_webhook(
                agent.id, request_for(payload, {"x-signature-timestamp": stamp, "x-signature-ed25519": signature})
            )
        case "feishu" | "feishu_image" | "feishu_file":
            config.app_id, config.app_secret = "app-id", "app-secret"
            msg_type = "text" if name == "feishu" else name.removeprefix("feishu_")
            content = (
                {"text": "hello"}
                if msg_type == "text"
                else {"image_key": "image-key", "file_key": "file-key", "file_name": "upload.bin"}
            )
            await feishu.process_feishu_event(
                agent.id,
                {
                    "header": {"event_type": "im.message.receive_v1"},
                    "event": {
                        "sender": {"sender_id": {"open_id": "ou_open", "user_id": "tenant-user"}},
                        "message": {
                            "message_type": msg_type,
                            "chat_type": "group" if group else "p2p",
                            "chat_id": "oc_chat",
                            "content": json.dumps(content),
                        },
                    },
                },
            )
        case "google_inline":
            await google_chat.google_chat_event_webhook(
                agent.id,
                request_for(
                    {
                        "token": "verification-token",
                        "type": "MESSAGE",
                        "message": {
                            "text": "hello",
                            "sender": {"name": "users/123", "type": "HUMAN"},
                            "space": {"name": "spaces/ABC", "type": "ROOM" if group else "DM"},
                        },
                    }
                ),
            )
        case _:
            raise AssertionError(name)
    for coroutine in scheduled:
        await coroutine
    scheduled.clear()
