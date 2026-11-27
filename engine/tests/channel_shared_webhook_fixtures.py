from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest
from nacl.signing import SigningKey
from starlette.requests import Request

from app.api import discord_bot, feishu, google_chat, slack, teams, whatsapp
from app.core.json_types import JsonObject
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.services.channels import feishu_submissions, slack_submissions
from test_channel_shared_processors import context, pipeline

__all__ = ["context", "pipeline"]

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
    "google_chat",
]


@pytest.fixture
def webhook_pipeline(pipeline, monkeypatch):
    _, _, _, _, config, *_ = pipeline
    config.is_configured = True
    config.encrypt_key = "signing-secret"
    config.verification_token = "verification-token"
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat.channel_config, "get_channel_config", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat.gchat, "audience_for_config", lambda _: "")
    monkeypatch.setattr(chat_message_dao, "insert_message", AsyncMock())
    monkeypatch.setattr(chat_session_dao, "update", AsyncMock())
    monkeypatch.setattr(
        feishu_submissions.feishu_service, "download_message_resource", AsyncMock(return_value=b"image-bytes")
    )
    upload = AsyncMock(return_value=("key", "workspace/uploads/upload.bin", "path"))
    monkeypatch.setattr(slack_submissions, "store_agent_upload", upload)
    monkeypatch.setattr(feishu_submissions, "store_agent_upload", upload)
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


async def invoke_webhook(name, webhook_pipeline, monkeypatch, *, group=False, native_id="native-id"):
    pipeline, scheduled = webhook_pipeline
    agent, _, _, _, config, *_ = pipeline
    config.channel_type = (
        "slack"
        if name.startswith("slack")
        else "feishu"
        if name.startswith("feishu")
        else "microsoft_teams"
        if name == "teams"
        else name
    )
    match name:
        case "slack" | "slack_upload" | "slack_file_only":
            event: JsonObject = {
                "type": "message",
                "user": "U123",
                "channel": "C123" if group else "D123",
                "text": "" if name == "slack_file_only" else "hello",
                "ts": native_id,
            }
            if name != "slack":
                event["files"] = [
                    {"id": "native-file", "name": "upload.bin", "url_private_download": "https://provider.invalid/file"}
                ]
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
            config.app_id = "phone-account"
            payload = {
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "metadata": {"phone_number_id": "phone-account"},
                                    "messages": [
                                        {
                                            "id": native_id,
                                            "from": "15551234567",
                                            "type": "text",
                                            "timestamp": str(int(time.time()) - 1),
                                            "text": {"body": "hello"},
                                        }
                                    ],
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
                "id": native_id,
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
                "id": native_id,
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
                            "message_id": native_id,
                            "message_type": msg_type,
                            "chat_type": "group" if group else "p2p",
                            "chat_id": "oc_chat",
                            "content": json.dumps(content),
                        },
                    },
                },
            )
        case "google_chat":
            await google_chat.google_chat_event_webhook(
                agent.id,
                request_for(
                    {
                        "token": "verification-token",
                        "type": "MESSAGE",
                        "message": {
                            "name": native_id,
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
