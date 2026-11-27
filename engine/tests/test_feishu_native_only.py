from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from lark_oapi.ws.client import Client
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from app.api import feishu as feishu_api
from app.dao import agent_dao
from app.services.channels import binding, feishu_submissions, submissions
from app.services.channels.feishu_ws_acceptance import accept_event_frame
from app.services.channels.stream_source import stream_source
from app.services.feishu_ws import FeishuWSManager
from channel_media_fixtures import REAL_CLIENT
from test_channel_shared_processors import context, pipeline
from test_feishu_acceptance_ack import event_frame
from test_feishu_inbound import IMAGE_KEY, feishu_acceptance, feishu_event, serve_resource

__all__ = ["context", "feishu_acceptance", "pipeline"]

CONTENT = {
    "text": {"text": "hello"},
    "post": {"title": "", "content": [[{"tag": "text", "text": "hello"}, {"tag": "img", "image_key": IMAGE_KEY}]]},
    "image": {"image_key": IMAGE_KEY},
    "file": {"file_key": "file_key_00000000", "file_name": "report.pdf"},
    "media": {"file_key": "file_key_00000000"},
}
MESSAGE = "im.message.receive_v1"


def delivery(event, event_type=MESSAGE):
    return {"header": {"event_id": "evt-1", "event_type": event_type}, "event": event}


def without_id(kind):
    event = feishu_event(kind, CONTENT.get(kind, {}))
    del event["message"]["message_id"]
    return event


@pytest.fixture
def intake(feishu_acceptance, monkeypatch):
    """Feishu acceptance with every resource request and reply captured."""
    config, upload, persist, enqueue = feishu_acceptance
    config.verification_token, config.encrypt_key = "verification-token", ""
    requested = []
    serve_resource(monkeypatch, lambda request: requested.append(request.url.path) or httpx.Response(200, content=b"x"))
    send = AsyncMock(return_value={})
    monkeypatch.setattr(feishu_submissions.feishu_service, "send_message", send)
    return SimpleNamespace(
        config=config, upload=upload, persist=persist, enqueue=enqueue, requested=requested, send=send
    )


def assert_nothing_accepted(intake):
    assert intake.requested == []
    intake.upload.assert_not_awaited()
    intake.persist.assert_not_awaited()
    intake.enqueue.assert_not_awaited()
    intake.send.assert_not_awaited()
    assert isinstance(submissions.channel_event_receipt_dao.find, AsyncMock)
    assert isinstance(submissions.channel_event_receipt_dao.reserve, AsyncMock)
    submissions.channel_event_receipt_dao.find.assert_not_awaited()
    submissions.channel_event_receipt_dao.reserve.assert_not_awaited()


def assert_no_agent_load():
    # Loading the model starts with loading the agent, and so does authorizing the event.
    assert isinstance(agent_dao.get, AsyncMock)
    agent_dao.get.assert_not_awaited()


@pytest.fixture
def webhook(intake):
    app = FastAPI()
    app.include_router(feishu_api.router, prefix="/api")

    async def deliver(payload):
        body = json.dumps(
            {"schema": "2.0", **payload, "header": {**payload["header"], "token": "verification-token"}}
        ).encode()
        headers = {
            "x-lark-request-timestamp": "123",
            "x-lark-request-nonce": "nonce",
            "x-lark-signature": hashlib.sha256(b"123nonce" + body).hexdigest(),
        }
        async with REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                f"/api/channel/feishu/{intake.config.agent_id}/webhook", content=body, headers=headers
            )

    return deliver


@pytest.fixture
def websocket(intake, monkeypatch):
    intake.config.extra_config = {**(intake.config.extra_config or {}), "connection_mode": "websocket"}
    source, manager, acknowledgements = stream_source(intake.config), FeishuWSManager(), []

    async def write(data):
        frame = Frame()
        frame.ParseFromString(data)
        acknowledgements.append(json.loads(frame.payload)["code"])

    async def deliver(payload):
        client = Client("app", "secret")  # the SDK client starts its cache cron, which needs the running loop
        monkeypatch.setattr(client, "_write_message", write)
        frame = event_frame(json.dumps(payload).encode())
        await accept_event_frame(client, frame, lambda body: manager._async_handle_message(source, body))
        return acknowledgements[-1]

    return deliver


@pytest.fixture(params=["webhook", "websocket"])
def deliver(request):
    """Delivers an event over either transport and returns the status Feishu would see: HTTP status or frame code."""

    async def status(payload):
        result = await request.getfixturevalue(request.param)(payload)
        return result.status_code if isinstance(result, httpx.Response) else result

    return status


@pytest.mark.parametrize("kind", list(CONTENT))
async def test_webhook_refuses_a_message_without_an_id_before_any_work(intake, webhook, kind):
    # Given a message of a supported kind that carries no message_id
    # When
    response = await webhook(delivery(without_id(kind)))
    # Then the delivery is a 400 and nothing was downloaded, loaded, accepted or sent
    assert response.status_code == 400
    assert response.json() == {"detail": "Feishu message ID is required"}
    assert_nothing_accepted(intake)
    assert_no_agent_load()


@pytest.mark.parametrize("kind", list(CONTENT))
async def test_websocket_acknowledges_a_message_without_an_id_with_a_warning(intake, websocket, monkeypatch, kind):
    # Given the same message over the long connection, where a raised error makes the SDK answer 500 and Feishu resend it
    logs = Mock()
    monkeypatch.setattr(feishu_api, "logger", logs)
    # When
    code = await websocket(delivery(without_id(kind)))
    # Then it is acknowledged, logged once as a warning for the agent, and nothing was processed
    assert code == 200
    logs.warning.assert_called_once()
    assert logs.warning.call_args.args[1] == intake.config.agent_id
    assert_nothing_accepted(intake)
    assert_no_agent_load()


@pytest.mark.parametrize("with_id", [True, False])
async def test_unsupported_message_kind_is_acknowledged_without_acceptance(intake, deliver, with_id):
    # Given a sticker, a kind Feishu delivers but the agent cannot read, with and without an id
    event = feishu_event("sticker", {"file_key": "sticker_key"})
    if not with_id:
        del event["message"]["message_id"]
    # When / Then
    assert await deliver(delivery(event)) == 200
    assert_nothing_accepted(intake)
    assert_no_agent_load()


async def test_non_message_event_is_acknowledged_without_acceptance(intake, deliver):
    # Given the bot being added to a group
    # When / Then
    assert await deliver(delivery({"chat_id": "oc_chat"}, "im.chat.member.bot.added_v1")) == 200
    assert_nothing_accepted(intake)
    assert_no_agent_load()


@pytest.mark.parametrize("kind", ["text", "image"])
async def test_unlinked_sender_is_acknowledged_without_a_reply_or_any_state(intake, deliver, monkeypatch, kind):
    # Given a sender whose Feishu identity is not linked to a member
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    # When / Then the delivery is acknowledged, so Feishu does not resend it, and nothing was fetched, stored or sent
    assert await deliver(delivery(feishu_event(kind, CONTENT[kind]))) == 200
    assert_nothing_accepted(intake)


async def test_text_message_is_accepted_through_native_acceptance(intake, deliver):
    # Given a text message with a message_id
    # When
    assert await deliver(delivery(feishu_event("text", CONTENT["text"]))) == 200
    # Then it is a receipt keyed by app, chat and message, persisted and enqueued once
    reserve = submissions.channel_event_receipt_dao.reserve
    assert isinstance(reserve, AsyncMock)
    reserve.assert_awaited_once()
    assert reserve.await_args is not None
    assert reserve.await_args.args[0].event_key == json.dumps(["app-id", "oc_chat", "om_native"], separators=(",", ":"))
    assert intake.persist.await_args.kwargs["content"] == "hello"
    intake.enqueue.assert_awaited_once()
