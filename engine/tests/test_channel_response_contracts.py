import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from types import ModuleType
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from nacl.signing import SigningKey
from pydantic import TypeAdapter, ValidationError
from tests.im_webhook_fixtures import AGENT_ID, channel_config
from typing_extensions import TypeForm

from app.api import discord_bot, feishu, google_chat, messages, slack, teams, webhooks, wecom, whatsapp
from app.core.security import get_current_user
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.schemas import channel_response_contracts as contracts
from app.schemas.content_response_catalog import OkAcknowledgement
from app.schemas.notification_responses import NotificationUnreadCountResponse
from app.services.platform_service import platform_service

FIXED_CASES = [
    (contracts.GatewayHeartbeatResponse, {"status": "ok", "agent_id": "agent"}),
    (
        contracts.GatewaySendMessageResponse,
        {"status": "accepted", "target": "Peer", "type": "openclaw_agent", "message": "queued"},
    ),
    (
        contracts.GatewaySendMessageResponse,
        {"status": "sent", "target": "Member", "type": "human", "channel": "feishu"},
    ),
    (
        contracts.GatewaySetupGuideResponse,
        {"skill_filename": "sync.md", "skill_content": "", "heartbeat_addition": ""},
    ),
    (
        contracts.MessageInboxItem,
        {
            "id": "message",
            "sender_type": "agent",
            "sender_name": "Peer",
            "content": "",
            "session_title": "",
            "created_at": None,
        },
    ),
    (contracts.WebhookUrlResponse, {"webhook_url": "https://example.test/callback"}),
    (contracts.DiscordInteractionResponse, {"type": 1}),
    (contracts.DiscordInteractionResponse, {"type": 5}),
    (contracts.DiscordInteractionResponse, {"type": 4, "data": {"content": ""}}),
    (contracts.FeishuChallengeResponse, {"challenge": "verify"}),
    (contracts.FeishuEventResponse, {"code": 0, "msg": "ok"}),
    (contracts.FeishuEventResponse, {"code": 1, "msg": ""}),
    (contracts.ScheduleTriggerResponse, {"status": "triggered", "schedule_id": "schedule"}),
    (contracts.TaskTriggerResponse, {"status": "triggered", "task_id": "task"}),
    (contracts.UserRoleResponse, {"status": "ok", "user_id": "user", "role": "future_role"}),
    (OkAcknowledgement, {"ok": True}),
    (NotificationUnreadCountResponse, {"unread_count": 0}),
]


@pytest.mark.parametrize(("contract", "payload"), FIXED_CASES)
def test_adapters_preserve_emitted_payloads(contract: TypeForm[object], payload: dict[str, object]) -> None:
    adapter = TypeAdapter[object](contract)

    result = adapter.validate_json(json.dumps(payload))

    assert result == payload


@pytest.mark.parametrize(
    ("contract", "payload", "missing"),
    [(contract, payload, key) for contract, payload in FIXED_CASES for key in payload],
)
def test_adapters_require_emitted_fields(contract: TypeForm[object], payload: dict[str, object], missing: str) -> None:
    incomplete = dict(payload)
    del incomplete[missing]

    with pytest.raises(ValidationError):
        TypeAdapter[object](contract).validate_python(incomplete)


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (contracts.StatusAcknowledgement, {"status": "pending"}),
        (contracts.DiscordInteractionResponse, {"type": 4}),
        (contracts.DiscordInteractionResponse, {"type": 2}),
        (contracts.FeishuChallengeResponse, {"challenge": None}),
        (contracts.TaskTriggerResponse, {"status": "pending", "task_id": "task"}),
        (OkAcknowledgement, {"ok": False}),
    ],
)
def test_adapters_reject_incompatible_variants(contract: TypeForm[object], payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter[object](contract).validate_python(payload)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    for router in (
        discord_bot.router,
        feishu.router,
        google_chat.router,
        messages.router,
        slack.router,
        teams.router,
        wecom.router,
        whatsapp.router,
    ):
        application.include_router(router, prefix="/api")
    application.include_router(webhooks.router)
    application.dependency_overrides[get_current_user] = lambda: UserRecord(id=AGENT_ID)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as session:
            yield session
    finally:
        application.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("module", "endpoint", "provider"),
    [
        (discord_bot, "discord-channel", "discord"),
        (feishu, "channel", "feishu"),
        (google_chat, "google-chat-channel", "google-chat"),
        (slack, "slack-channel", "slack"),
        (teams, "teams-channel", "teams"),
        (wecom, "wecom-channel", "wecom"),
        (whatsapp, "whatsapp-channel", "whatsapp"),
    ],
)
async def test_webhook_urls_keep_the_existing_envelope(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, module: ModuleType, endpoint: str, provider: str
) -> None:
    monkeypatch.setattr(
        module,
        "check_agent_access",
        AsyncMock(return_value=(AgentRecord(id=AGENT_ID, name="Agent", creator_id=AGENT_ID), "manage")),
    )
    monkeypatch.setattr(platform_service, "get_public_base_url", AsyncMock(return_value="https://public.test"))

    response = await client.get(f"/api/agents/{AGENT_ID}/{endpoint}/webhook-url")

    assert response.status_code == 200
    assert TypeAdapter(contracts.WebhookUrlResponse).validate_json(response.content) == {
        "webhook_url": f"https://public.test/api/channel/{provider}/{AGENT_ID}/webhook"
    }


@pytest.mark.parametrize("challenge", ["verify", None, {"nested": [1, None]}])
async def test_slack_echoes_json_challenges_without_coercion(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, challenge: object
) -> None:
    config = channel_config("slack")
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    body = json.dumps({"type": "url_verification", "challenge": challenge}).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"app-secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()

    response = await client.post(
        f"/api/channel/slack/{AGENT_ID}/webhook",
        content=body,
        headers={"x-slack-request-timestamp": timestamp, "x-slack-signature": f"v0={signature}"},
    )

    assert response.status_code == 200
    assert TypeAdapter(contracts.SlackChallengeResponse).validate_json(response.content) == {"challenge": challenge}


@pytest.mark.parametrize(("interaction_type", "response_type"), [(1, 1), (9, 1), (2, 4)])
async def test_signed_discord_interaction_variants_remain_direct_json(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, interaction_type: int, response_type: int
) -> None:
    signing_key = SigningKey.generate()
    config = channel_config("discord")
    config.encrypt_key = signing_key.verify_key.encode().hex()
    monkeypatch.setattr(discord_bot.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    body = json.dumps({"type": interaction_type, "data": {"options": []}}).encode()
    signature = signing_key.sign(b"123" + body).signature.hex()

    response = await client.post(
        f"/api/channel/discord/{AGENT_ID}/webhook",
        content=body,
        headers={"x-signature-timestamp": "123", "x-signature-ed25519": signature},
    )

    assert response.status_code == 200
    payload = TypeAdapter(contracts.DiscordInteractionResponse).validate_json(response.content)
    assert payload["type"] == response_type


async def test_inbox_keeps_required_null_timestamp(monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient) -> None:
    session = ChatSessionRecord(id=uuid.uuid4(), agent_id=AGENT_ID, user_id=AGENT_ID)
    message = ChatMessageRecord(id=uuid.uuid4(), agent_id=AGENT_ID, user_id=AGENT_ID, role="assistant", content="hello")
    monkeypatch.setattr(messages.agent_dao, "list_ids_for_creator", AsyncMock(return_value=[AGENT_ID]))
    monkeypatch.setattr(messages.chat_session_dao, "list_agent_channel_sessions", AsyncMock(return_value=[session]))
    monkeypatch.setattr(
        messages.chat_message_dao, "list_latest_for_conversations", AsyncMock(return_value={str(session.id): [message]})
    )
    monkeypatch.setattr(messages.participant_dao, "get_many", AsyncMock(return_value=[]))

    response = await client.get("/api/messages/inbox")

    assert response.status_code == 200
    assert TypeAdapter(list[contracts.MessageInboxItem]).validate_json(response.content) == [
        {
            "id": str(message.id),
            "sender_type": "agent",
            "sender_name": "Unknown",
            "content": "hello",
            "session_title": "New Session",
            "created_at": None,
        }
    ]


@pytest.mark.parametrize("valid", [True, False])
async def test_whatsapp_verification_keeps_plain_transport(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, valid: bool
) -> None:
    monkeypatch.setattr(
        whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("whatsapp"))
    )

    response = await client.get(
        f"/api/channel/whatsapp/{AGENT_ID}/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verification-token" if valid else "wrong",
            "hub.challenge": "12345",
        },
    )

    assert response.status_code == (200 if valid else 403)
    assert response.content == (b"12345" if valid else b"")
    if valid:
        assert response.headers["content-type"] == "text/plain; charset=utf-8"


async def test_wecom_encrypted_xml_keeps_plain_ack(monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient) -> None:
    config = channel_config("wecom")
    config.encrypt_key = base64.b64encode(bytes(range(32))).decode().rstrip("=")
    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    encrypted = wecom._encrypt_msg(config.encrypt_key, "<xml><MsgType>image</MsgType></xml>", "corp")
    signature = wecom._verify_signature("verification-token", "123", "nonce", encrypted)

    response = await client.post(
        f"/api/channel/wecom/{AGENT_ID}/webhook",
        content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
        params={"msg_signature": signature, "timestamp": "123", "nonce": "nonce"},
    )

    assert response.status_code == 200
    assert response.content == b"success"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"


async def test_channel_delete_keeps_empty_204(monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient) -> None:
    monkeypatch.setattr(
        whatsapp,
        "check_agent_access",
        AsyncMock(return_value=(AgentRecord(id=AGENT_ID, name="Agent", creator_id=AGENT_ID), "manage")),
    )
    monkeypatch.setattr(
        whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("whatsapp"))
    )
    monkeypatch.setattr(whatsapp.channel_config_dao, "delete", AsyncMock())

    response = await client.delete(f"/api/agents/{AGENT_ID}/whatsapp-channel")

    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.parametrize("hits", [0, 60])
async def test_webhook_json_response_keeps_status_and_headers(
    monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient, hits: int
) -> None:
    monkeypatch.setattr(webhooks, "_record_and_count_hits", AsyncMock(return_value=hits))
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=None))

    response = await client.post("/api/webhooks/t/unknown", content=b"raw body")

    assert response.status_code == (200 if hits == 0 else 429)
    assert TypeAdapter(OkAcknowledgement).validate_json(response.content) == {"ok": True}
    assert response.headers["content-type"] == "application/json"
