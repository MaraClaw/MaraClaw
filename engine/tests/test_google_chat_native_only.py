from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from app.api import google_chat as google_chat_api
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject, JsonValue
from app.dao import agent_dao
from app.records.channel_event_receipt import ChannelEventReceiptRecord
from app.services.channels import binding, google_chat_media, submissions
from test_channel_shared_processors import context, pipeline

__all__ = ["context", "pipeline"]

APP_ID = "chat-project"
MESSAGE_NAME = "spaces/ABC/messages/XYZ"
ATTACHMENT: JsonObject = {
    "name": f"{MESSAGE_NAME}/attachments/A",
    "attachmentDataRef": {"resourceName": f"{MESSAGE_NAME}/attachments/A"},
}


def delivery(kind: str = "MESSAGE", /, **message: JsonValue) -> JsonObject:
    return {
        "type": kind,
        "token": "verification-token",
        "message": {
            "name": MESSAGE_NAME,
            "text": "hello",
            "sender": {"name": "users/123", "type": "HUMAN"},
            "space": {"name": "spaces/ABC", "type": "DM"},
            **message,
        },
    }


def without_name(payload: JsonObject) -> JsonObject:
    assert isinstance(payload["message"], dict)
    del payload["message"]["name"]
    return payload


@pytest.fixture
def intake(pipeline, monkeypatch):
    """The Google Chat webhook over real native acceptance, with every download, store, write and enqueue captured."""
    agent, _, _, _, config, _, persist, enqueue = pipeline
    config.channel_type, config.app_id, config.verification_token = (
        ChannelType.GOOGLE_CHAT,
        APP_ID,
        "verification-token",
    )
    monkeypatch.setattr(google_chat_api.channel_config, "get_channel_config", AsyncMock(return_value=config))
    monkeypatch.setattr(google_chat_api.gchat, "audience_for_config", lambda _: "")
    download, store = AsyncMock(), AsyncMock()
    monkeypatch.setattr(google_chat_media, "_download", download)
    monkeypatch.setattr(google_chat_media, "store_agent_upload", store)
    app = FastAPI()
    app.include_router(google_chat_api.router, prefix="/api")

    async def deliver(payload: JsonObject) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(f"/api/channel/google-chat/{agent.id}/webhook", json=payload)

    return SimpleNamespace(
        agent=agent, config=config, persist=persist, enqueue=enqueue, download=download, store=store, deliver=deliver
    )


def assert_nothing_accepted(intake) -> None:
    intake.download.assert_not_awaited()
    intake.store.assert_not_awaited()
    intake.persist.assert_not_awaited()
    intake.enqueue.assert_not_awaited()
    assert isinstance(submissions.channel_event_receipt_dao.find, AsyncMock)
    assert isinstance(submissions.channel_event_receipt_dao.reserve, AsyncMock)
    submissions.channel_event_receipt_dao.find.assert_not_awaited()
    submissions.channel_event_receipt_dao.reserve.assert_not_awaited()


@pytest.mark.parametrize("kind", ["MESSAGE", "ADDED_TO_SPACE"])
@pytest.mark.parametrize("attachment", [False, True])
async def test_message_without_a_name_is_refused_before_any_work(intake, kind, attachment):
    # Given a message, or the one an ADDED_TO_SPACE event carries, with a body or an attachment but no message.name
    fields: JsonObject = {"text": "", "attachment": [ATTACHMENT]} if attachment else {}
    payload = without_name(delivery(kind, **fields))
    # When
    response = await intake.deliver(payload)
    # Then the delivery is a 400 and nothing was resolved, downloaded, stored, accepted or enqueued
    assert response.status_code == 400
    assert response.json() == {"detail": "Google Chat message name is required"}
    assert_nothing_accepted(intake)
    assert isinstance(agent_dao.get, AsyncMock)
    agent_dao.get.assert_not_awaited()


@pytest.mark.parametrize("kind", ["MESSAGE", "ADDED_TO_SPACE"])
async def test_message_without_text_or_attachment_is_acknowledged_without_acceptance(intake, kind):
    # Given a named message from a known sender that carries neither text nor an attachment
    # When
    response = await intake.deliver(delivery(kind, text="", attachment=[]))
    # Then it is acknowledged (MESSAGE) or answered as membership (ADDED_TO_SPACE) and nothing was accepted
    assert response.status_code == 200
    assert (response.json() == {}) == (kind == "MESSAGE")
    assert_nothing_accepted(intake)


@pytest.mark.parametrize(
    ("kind", "answer"),
    [("REMOVED_FROM_SPACE", False), ("ADDED_TO_SPACE", True), ("CARD_CLICKED", False), ("APP_COMMAND", False)],
)
async def test_membership_and_other_events_never_reach_acceptance(intake, kind, answer):
    # Given an event without a message
    payload: JsonObject = {"type": kind, "token": "verification-token", "space": {"name": "spaces/ABC", "type": "DM"}}
    # When
    response = await intake.deliver(payload)
    # Then ADDED_TO_SPACE is welcomed inline, every other type is acknowledged, and nothing was accepted
    assert response.status_code == 200
    assert bool(response.json()) is answer
    assert_nothing_accepted(intake)


@pytest.mark.parametrize("attachment", [False, True])
async def test_unlinked_sender_is_acknowledged_without_any_state(intake, monkeypatch, attachment):
    # Given a sender whose Google Chat identity is not linked to a member
    monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    logs = Mock()
    monkeypatch.setattr(google_chat_api, "logger", logs)
    fields: JsonObject = {"text": "", "attachment": [ATTACHMENT]} if attachment else {}
    payload = delivery(**fields)
    # When
    response = await intake.deliver(payload)
    # Then Google gets a 200 and nothing was downloaded, stored, reserved or enqueued; one info line names the agent only
    assert response.status_code == 200
    assert response.json() == {}
    assert_nothing_accepted(intake)
    logs.info.assert_called_once()
    assert logs.info.call_args.args[1] == intake.agent.id
    assert "hello" not in repr(logs.mock_calls)


async def test_named_message_is_accepted_once_and_its_redelivery_replays(intake, monkeypatch):
    # Given acceptance that reserves a receipt on the first delivery and finds it on the next
    reserved: list[ChannelEventReceiptRecord] = []

    async def reserve(candidate: ChannelEventReceiptRecord) -> bool:
        reserved.append(candidate)
        return len(reserved) == 1

    async def find(_candidate: ChannelEventReceiptRecord) -> ChannelEventReceiptRecord:
        return reserved[0]

    monkeypatch.setattr(submissions.channel_event_receipt_dao, "reserve", reserve)
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "find", find)
    # When Google delivers the same message twice
    first, second = await intake.deliver(delivery()), await intake.deliver(delivery())
    # Then both answer 200, the receipt is keyed by project, space and message name, and the turn ran once
    assert (first.status_code, first.json(), second.status_code, second.json()) == (200, {}, 200, {})
    assert [receipt.event_key for receipt in reserved] == [
        json.dumps([APP_ID, "spaces/ABC", MESSAGE_NAME], separators=(",", ":"))
    ] * 2
    intake.persist.assert_awaited_once()
    intake.enqueue.assert_awaited_once()
    assert intake.persist.await_args.kwargs["content"] == "hello"
