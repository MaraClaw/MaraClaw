from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import telegram
from app.core.domain.channels import ChannelType
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE
from app.records.channel_config import ChannelConfigRecord
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEvent, ChannelEventConflictError

CONFIG_ID = UUID("11111111-1111-4111-8111-111111111111")
AGENT_ID = UUID("22222222-2222-4222-8222-222222222222")
HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "webhook_secret"}
PATH = f"/api/channel/telegram/{CONFIG_ID}/webhook"


def native_message() -> dict[str, object]:
    return {
        "message_id": 9,
        "date": 1794758400,
        "from": {"id": 4503599627370495, "is_bot": False, "username": "renamable", "first_name": "Person"},
        "chat": {"id": 4503599627370495, "type": "private", "username": "renamable"},
        "text": " native text ",
    }


@pytest.fixture
async def webhook_api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncMock, ChannelConfigRecord]]:
    config = ChannelConfigRecord(
        id=CONFIG_ID,
        agent_id=AGENT_ID,
        channel_type=ChannelType.TELEGRAM,
        app_id="123456",
        app_secret="123456:secret",
        verification_token="webhook_secret",
        is_configured=True,
    )
    monkeypatch.setattr(telegram.channel_config_dao, "get", AsyncMock(return_value=config))
    accept = AsyncMock(return_value=AcceptedChannelEvent(replayed=False))
    monkeypatch.setattr(telegram, "accept_channel_event", accept)
    app = FastAPI()
    app.include_router(telegram.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        yield client, accept, config


async def test_native_private_text_uses_numeric_ids_and_delegates_every_retry(webhook_api) -> None:
    client, accept, _ = webhook_api
    update = {"update_id": 987654321, "message": native_message(), "future_field": True}
    for replayed in (False, True):
        accept.return_value = AcceptedChannelEvent(replayed=replayed)
        response = await client.post(PATH, json=update, headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
    assert accept.await_count == 2
    event = accept.await_args_list[0].args[0]
    assert isinstance(event, ChannelEvent)
    assert event.config_id == CONFIG_ID
    assert event.agent_id == AGENT_ID
    assert event.provider == "telegram"
    assert event.event_key == "123456:4503599627370495:9"
    assert event.sender_id == "4503599627370495"
    assert event.conversation_id == "telegram_4503599627370495"
    assert event.text == " native text "
    assert event.kind == "text"
    assert not event.is_group
    assert event.provider_account_id == "123456"
    assert event.provider_timestamp == datetime.fromtimestamp(1794758400, UTC)
    assert event.fingerprint() == accept.await_args_list[1].args[0].fingerprint()


@pytest.mark.parametrize("secret", [None, "", "wrong_secret"])
async def test_unauthenticated_updates_reject_before_parsing_or_acceptance(webhook_api, secret: str | None) -> None:
    client, accept, _ = webhook_api
    headers = {} if secret is None else {"X-Telegram-Bot-Api-Secret-Token": secret}
    response = await client.post(PATH, content=b"not-json", headers=headers)
    assert response.status_code == 401
    accept.assert_not_awaited()


@pytest.mark.parametrize(
    "patch",
    [
        {"from": {"id": 42, "is_bot": True}},
        {"from": {"id": 123456, "is_bot": False}},
        {"from": None},
        {"from": {"id": 42, "is_bot": False}},
        {"sender_chat": {"id": -42, "type": "channel"}},
        {"chat": {"id": -42, "type": "group"}},
        {"chat": {"id": -42, "type": "supergroup"}},
        {"chat": {"id": -42, "type": "channel"}},
        {"text": None, "photo": [{"file_id": "photo-id"}]},
        {"text": " \n "},
    ],
)
async def test_unsupported_messages_ack_without_submission(webhook_api, patch: dict[str, object]) -> None:
    client, accept, _ = webhook_api
    response = await client.post(PATH, json={"update_id": 5, "message": {**native_message(), **patch}}, headers=HEADERS)
    assert response.status_code == 200
    accept.assert_not_awaited()


async def test_nonmessage_update_acknowledges_and_optional_date_is_supported(webhook_api) -> None:
    client, accept, _ = webhook_api
    assert (await client.post(PATH, json={"update_id": 5, "callback_query": {}}, headers=HEADERS)).status_code == 200
    accept.assert_not_awaited()
    message = native_message()
    del message["date"]
    assert (await client.post(PATH, json={"update_id": 6, "message": message}, headers=HEADERS)).status_code == 200
    assert accept.await_args.args[0].provider_timestamp is None


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": True},
        {"update_id": "42"},
        {},
        [],
        {"update_id": 42, "message": {**native_message(), "from": {"id": "42", "is_bot": False}}},
        {"update_id": 42, "message": {**native_message(), "chat": {"id": True, "type": "private"}}},
        {"update_id": 42, "message": {**native_message(), "text": 42}},
        {"update_id": 42, "message": {**native_message(), "date": 10**20}},
    ],
)
async def test_malformed_authenticated_updates_are_typed_rejections(webhook_api, update: object) -> None:
    client, accept, _ = webhook_api
    response = await client.post(PATH, json=update, headers=HEADERS)
    assert response.status_code == 400
    accept.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (HTTPException(403, "access revoked"), 403),
        (HTTPException(503, "retry"), 503),
        (RuntimeError("database unavailable"), 500),
    ],
)
async def test_acceptance_failures_are_never_acknowledged(webhook_api, error: Exception, status: int) -> None:
    client, accept, _ = webhook_api
    accept.side_effect = error
    response = await client.post(PATH, json={"update_id": 7, "message": native_message()}, headers=HEADERS)
    assert response.status_code == status


@pytest.mark.parametrize(
    "error", [ChannelUserResolutionError("unbound"), ChannelEventConflictError(CONFIG_ID, "telegram", "7")]
)
async def test_unbound_and_conflicting_messages_acknowledge_without_retry(webhook_api, error: Exception) -> None:
    client, accept, _ = webhook_api
    accept.side_effect = error
    response = await client.post(PATH, json={"update_id": 7, "message": native_message()}, headers=HEADERS)
    assert response.status_code == 200
    if isinstance(error, ChannelUserResolutionError):
        assert response.json()["method"] == "sendMessage"
        assert response.json()["chat_id"] == "4503599627370495"
    else:
        assert response.json() == {"ok": True}


@pytest.mark.parametrize("command", ["/start", "/start 01234ABCDE", "/link 01234ABCDE"])
async def test_link_commands_reply_without_acceptance(webhook_api, monkeypatch, command: str) -> None:
    client, accept, _ = webhook_api
    consume = AsyncMock(return_value="linked")
    monkeypatch.setattr(telegram, "consume_link_command", consume)
    response = await client.post(
        PATH, json={"update_id": 7, "message": {**native_message(), "text": command}}, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json()["method"] == "sendMessage"
    assert response.json()["chat_id"] == "4503599627370495"
    accept.assert_not_awaited()
    if command == "/start":
        consume.assert_not_awaited()
    else:
        assert consume.await_args is not None
        assert consume.await_args.args[1] == "01234ABCDE"


async def test_provider_message_id_survives_update_id_reuse(webhook_api) -> None:
    client, accept, _ = webhook_api
    for message_id in (9, 10):
        response = await client.post(
            PATH, json={"update_id": 7, "message": {**native_message(), "message_id": message_id}}, headers=HEADERS
        )
        assert response.status_code == 200
    assert accept.await_args_list[0].args[0].event_key != accept.await_args_list[1].args[0].event_key


async def test_message_id_is_required(webhook_api) -> None:
    client, accept, _ = webhook_api
    message = native_message()
    del message["message_id"]
    response = await client.post(PATH, json={"update_id": 7, "message": message}, headers=HEADERS)
    assert response.status_code == 400
    accept.assert_not_awaited()


async def test_wrong_provider_disabled_and_oversize_fail_closed(webhook_api) -> None:
    client, accept, config = webhook_api
    config.channel_type = ChannelType.SLACK
    assert (await client.post(PATH, json={"update_id": 7}, headers=HEADERS)).status_code == 404
    config.channel_type = ChannelType.TELEGRAM
    config.is_configured = False
    assert (await client.post(PATH, json={"update_id": 7}, headers=HEADERS)).status_code == 404
    config.is_configured = True
    response = await client.post(PATH, content=b"x" * (CHANNEL_WEBHOOK_MAX_BODY_SIZE + 1), headers=HEADERS)
    assert response.status_code == 413
    accept.assert_not_awaited()
