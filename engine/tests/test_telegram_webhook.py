from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import telegram
from app.core.domain.channels import ChannelType
from app.core.events import RedisPoolOverloadError
from app.core.request_body import CHANNEL_WEBHOOK_MAX_BODY_SIZE
from app.records.channel_config import ChannelConfigRecord
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import submissions, telegram_media
from app.services.channels.media_limits import READ_CHUNK_BYTES, deadline_note
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEvent, ChannelEventConflictError
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.local import LocalStorageBackend
from channel_media_fixtures import (
    ClaimStore,
    ClockedStream,
    GatedStream,
    SlowStream,
    StalledStream,
    ack_clock,
    assert_in_flight_refusal,
    claim_store,
    set_ack_budget,
    store_overrunning_the_deadline,
)

__all__ = ["ack_clock", "claim_store"]


@pytest.fixture(autouse=True)
def no_real_claims(claim_store: ClaimStore) -> ClaimStore:
    # Webhooks here take in-flight claims; on a real Redis one test's claim would answer a later test's delivery 503.
    return claim_store


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
        {"text": " \n "},
    ],
)
async def test_unsupported_messages_ack_without_submission(webhook_api, patch: dict[str, object]) -> None:
    client, accept, _ = webhook_api
    response = await client.post(PATH, json={"update_id": 5, "message": {**native_message(), **patch}}, headers=HEADERS)
    assert response.status_code == 200
    accept.assert_not_awaited()


@pytest.mark.parametrize("kind", ["photo", "document", "video"])
async def test_media_only_update_reaches_storage_and_replay_skips_download(webhook_api, monkeypatch, tmp_path, kind):
    from contextlib import asynccontextmanager

    from app.services.channels import submissions, telegram_media
    from app.services.storage_runtime import agent_files
    from app.services.storage_runtime.local import LocalStorageBackend

    client, accept, _ = webhook_api
    replay = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    calls = []

    def download(request):
        calls.append(request)
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "files/inbound"}})
        return httpx.Response(200, content=b"file bytes")

    @asynccontextmanager
    async def provider_client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as http:
            yield http

    monkeypatch.setattr(telegram_media, "public_http_client", provider_client)
    item = {"file_id": "media1", "file_name": "report.pdf" if kind == "document" else "clip.mp4"}
    message = {**native_message(), "text": None, kind: [item] if kind == "photo" else item}
    for _ in range(2):
        response = await client.post(PATH, json={"update_id": 9, "message": message}, headers=HEADERS)
        assert response.status_code == 200
    accept.assert_awaited_once()
    assert accept.await_args is not None
    event = accept.await_args.args[0]
    assert event.kind == kind
    assert event.media_ids == ("media1",)
    reference = event.attachments[0]
    assert await storage.read_bytes(f"{AGENT_ID}/{reference.path}") == b"file bytes"
    assert accept.await_args.kwargs["saved_content"] == f"[file:{reference.path}]"
    assert len(calls) == 2


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


MEDIA_UPDATE = {
    "update_id": 9,
    "message": {**native_message(), "text": None, "document": {"file_id": "media1", "file_name": "report.pdf"}},
}


@dataclass
class TelegramDownload:
    """Telegram's file API served from memory: ``serve_file`` answers the download, ``calls`` records every request."""

    replay: AsyncMock
    storage: LocalStorageBackend
    serve_file: Callable[[httpx.Request], httpx.Response]
    calls: list[httpx.Request] = field(default_factory=list)


@pytest.fixture
def telegram_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TelegramDownload:
    replay = AsyncMock(return_value=False)
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    download = TelegramDownload(replay, storage, lambda _: httpx.Response(200, content=b"file bytes"))

    def respond(request: httpx.Request) -> httpx.Response:
        download.calls.append(request)
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "files/inbound"}})
        return download.serve_file(request)

    @asynccontextmanager
    async def provider_client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            yield http

    monkeypatch.setattr(telegram_media, "public_http_client", provider_client)
    return download


async def test_stalled_telegram_file_host_is_cut_off_at_the_budget_and_the_message_is_accepted(
    webhook_api, telegram_download, ack_clock, monkeypatch
) -> None:
    # Given: a budget of a millisecond, and a file host that takes the request and then sends nothing
    client, accept, _ = webhook_api
    set_ack_budget(monkeypatch, 0.001)
    telegram_download.serve_file = lambda _: httpx.Response(200, stream=StalledStream())
    # When
    response = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: Telegram gets its usual answer, and the message is accepted with a note and no file
    assert (response.status_code, response.json()) == (200, {"ok": True})
    accept.assert_awaited_once()
    assert accept.await_args.args[0].attachments == ()
    assert accept.await_args.kwargs == {
        "content": deadline_note("report.pdf"),
        "saved_content": deadline_note("report.pdf"),
    }
    assert not await telegram_download.storage.list_dir(str(AGENT_ID))


async def test_slow_telegram_file_host_is_abandoned_at_the_budget_and_the_message_is_accepted(
    webhook_api, telegram_download, ack_clock, monkeypatch
) -> None:
    # Given: a 2.5 second budget, and a file the host serves at one second per chunk
    client, accept, _ = webhook_api
    set_ack_budget(monkeypatch, 2.5)
    stream = SlowStream(ack_clock, total=10 * READ_CHUNK_BYTES, step=1.0)
    telegram_download.serve_file = lambda _: httpx.Response(200, stream=stream)
    # When
    response = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: the read stopped at the first chunk that arrived late, and the message was accepted with a note
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert stream.consumed == 3 * READ_CHUNK_BYTES
    assert accept.await_args.args[0].attachments == ()
    assert accept.await_args.kwargs["content"] == deadline_note("report.pdf")


async def test_a_retried_media_update_while_the_first_is_downloading_is_refused_without_a_download(
    webhook_api, telegram_download, claim_store
) -> None:
    # Given: a first delivery held in its file download
    client, accept, _ = webhook_api
    gate = GatedStream()
    telegram_download.serve_file = lambda _: httpx.Response(200, stream=gate)
    first: list[httpx.Response] = []

    async def deliver() -> None:
        first.append(await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(deliver)
        await gate.started.wait()
        # When: Telegram redelivers the update while the first request is still working
        retry = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
        # Then: Telegram gets the retryable refusal, with no new download and nothing accepted yet
        assert_in_flight_refusal(retry)
        assert len(telegram_download.calls) == 2
        assert telegram_download.replay.await_count == 1
        accept.assert_not_awaited()
        gate.release.set()
    # And: the first delivery went on to accept the update once, then released the claim
    assert first[0].status_code == 200
    accept.assert_awaited_once()
    assert claim_store.claims == {}


async def test_a_media_update_is_not_lost_when_the_delivery_that_held_its_claim_fails(
    webhook_api, telegram_download, claim_store
) -> None:
    # Given: a first delivery held in its file download, whose acceptance will then fail
    client, accept, _ = webhook_api
    accept.side_effect = [HTTPException(503, "retry"), AcceptedChannelEvent(replayed=False)]
    gate = GatedStream()
    telegram_download.serve_file = lambda _: httpx.Response(200, stream=gate)
    first: list[httpx.Response] = []

    async def deliver() -> None:
        first.append(await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(deliver)
        await gate.started.wait()
        # And: Telegram's retry arrives meanwhile and is refused, so Telegram keeps the update pending
        assert_in_flight_refusal(await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS))
        gate.release.set()
    assert first[0].status_code == 503
    assert claim_store.claims == {}
    # When: Telegram repeats the update after the first delivery failed and released its claim
    retried = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: the repeat is processed in full, so the update is not lost
    assert (retried.status_code, retried.json()) == (200, {"ok": True})
    assert accept.await_count == 2


async def test_a_telegram_file_fetched_as_the_deadline_passes_is_not_stored_and_the_message_is_accepted(
    webhook_api, telegram_download, ack_clock, monkeypatch
) -> None:
    # Given: a 2 second budget, and a file whose body ends after it
    client, accept, _ = webhook_api
    set_ack_budget(monkeypatch, 2.0)
    telegram_download.serve_file = lambda _: httpx.Response(200, stream=ClockedStream(ack_clock, 3.0))
    # When
    response = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: the file was fetched but not stored, and the message was accepted with a note
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert not await telegram_download.storage.list_dir(str(AGENT_ID))
    assert accept.await_args.args[0].attachments == ()
    assert accept.await_args.kwargs["content"] == deadline_note("report.pdf")


async def test_a_telegram_store_that_starts_before_the_deadline_runs_to_completion_and_is_attached(
    webhook_api, telegram_download, ack_clock, monkeypatch
) -> None:
    # Given: a 2 second budget, and a store that starts in time and finishes after the deadline
    client, accept, _ = webhook_api
    set_ack_budget(monkeypatch, 2.0)
    store = store_overrunning_the_deadline(ack_clock, telegram_media.store_agent_upload)
    monkeypatch.setattr(telegram_media, "store_agent_upload", store)
    # When
    response = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: the file was stored once and attached, with no note and no other file left in the workspace
    assert (response.status_code, response.json()) == (200, {"ok": True})
    (reference,) = accept.await_args.args[0].attachments
    assert accept.await_args.kwargs["content"] == f"[file:{reference.path}]"
    assert await telegram_download.storage.read_bytes(f"{AGENT_ID}/{reference.path}") == b"file bytes"
    assert len(await telegram_download.storage.list_dir(f"{AGENT_ID}/workspace/uploads")) == 1


@pytest.mark.parametrize(
    "error", [ChannelUserResolutionError("unbound"), ChannelEventConflictError(CONFIG_ID, "telegram", "9")]
)
async def test_media_update_replay_failures_keep_their_answers_and_release_the_claim(
    webhook_api, telegram_download, claim_store, error: Exception
) -> None:
    # Given: the replay check refuses the sender, or finds a different event under the same key
    client, accept, _ = webhook_api
    telegram_download.replay.side_effect = error
    # When
    response = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: the status code and body are what they were without claims, nothing was downloaded, and no claim is left
    assert response.status_code == 200
    if isinstance(error, ChannelUserResolutionError):
        assert response.json()["method"] == "sendMessage"
    else:
        assert response.json() == {"ok": True}
    assert telegram_download.calls == []
    accept.assert_not_awaited()
    assert claim_store.claims == {}


async def test_a_failed_media_update_releases_its_claim_so_the_providers_retry_is_processed(
    webhook_api, telegram_download, claim_store
) -> None:
    # Given: an acceptance that fails once
    client, accept, _ = webhook_api
    accept.side_effect = [HTTPException(503, "retry"), AcceptedChannelEvent(replayed=False)]
    # When
    failed = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    assert claim_store.claims == {}
    retried = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: Telegram saw the failure, and its retry was processed in full
    assert (failed.status_code, retried.status_code) == (503, 200)
    assert accept.await_count == 2
    assert len(telegram_download.calls) == 4


@pytest.mark.parametrize("failure", [ConnectionError("redis down"), RedisPoolOverloadError("cache pool busy")])
async def test_an_unavailable_claim_store_falls_back_to_processing_the_media_update(
    webhook_api, telegram_download, claim_store, failure: Exception
) -> None:
    # Given: Redis is down, or its pool has no free connection
    client, accept, _ = webhook_api
    claim_store.failure = failure
    # When
    response = await client.post(PATH, json=MEDIA_UPDATE, headers=HEADERS)
    # Then: the update is processed as it was before claims existed
    assert (response.status_code, response.json()) == (200, {"ok": True})
    accept.assert_awaited_once()
    assert len(accept.await_args.args[0].attachments) == 1
