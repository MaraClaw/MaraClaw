import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import HTTPException
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import whatsapp
from app.core.events import RedisPoolOverloadError
from app.services.channels import submissions, whatsapp_cloud_media
from app.services.channels.media_limits import READ_CHUNK_BYTES, deadline_note
from app.services.channels.submissions import AcceptedChannelEvent, ChannelEventConflictError
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.local import LocalStorageBackend
from channel_media_fixtures import (
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


@pytest.mark.parametrize("kind", ["document", "image", "video"])
async def test_signed_media_only_message_stores_once_and_replay_skips_provider(monkeypatch, tmp_path, kind):
    config = channel_config("whatsapp")
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    accept = AsyncMock(return_value=AcceptedChannelEvent(replayed=False))
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    monkeypatch.setattr(submissions, "is_channel_event_replay", AsyncMock(side_effect=[False, True]))
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    calls = []

    def download(request):
        calls.append(request)
        if request.url.host == "graph.facebook.com":
            assert request.url.params["phone_number_id"] == config.app_id
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media1"})
        assert request.headers["Authorization"] == f"Bearer {config.app_secret}"
        return httpx.Response(200, content=b"file payload")

    @asynccontextmanager
    async def provider_client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as http:
            yield http

    monkeypatch.setattr(whatsapp_cloud_media, "public_http_client", provider_client)
    message = {
        "id": "native-media",
        "from": "15550001",
        "type": kind,
        kind: {"id": "media1", "filename": "report.xlsx"},
    }
    value = {"metadata": {"phone_number_id": config.app_id}, "messages": [message]}
    body = json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()}
    async with webhook_client(whatsapp.router) as client:
        for _ in range(2):
            response = await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=body, headers=headers)
            assert response.status_code == 200
    accept.assert_awaited_once()
    assert accept.await_args is not None
    event = accept.await_args.args[0]
    assert event.kind == kind
    assert event.media_ids == ("media1",)
    reference = event.attachments[0]
    assert reference.name == "report.xlsx"
    assert await storage.read_bytes(f"{AGENT_ID}/{reference.path}") == b"file payload"
    assert accept.await_args.kwargs["saved_content"] == f"[file:{reference.path}]"
    assert len(calls) == 2


async def test_unbound_signed_media_never_downloads_or_persists(monkeypatch):
    config = channel_config("whatsapp")
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(submissions, "is_channel_event_replay", AsyncMock(side_effect=HTTPException(403)))
    download = AsyncMock()
    accept = AsyncMock()
    monkeypatch.setattr(whatsapp_cloud_media, "enrich_media", download)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    message = {"id": "denied-media", "from": "15550001", "type": "image", "image": {"id": "media1"}}
    value = {"metadata": {"phone_number_id": config.app_id}, "messages": [message]}
    body = json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()}
    async with webhook_client(whatsapp.router) as client:
        response = await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=body, headers=headers)
    assert response.status_code == 200
    download.assert_not_awaited()
    accept.assert_not_awaited()


@dataclass
class CloudMedia:
    """A signed WhatsApp Cloud document message with its media API served from memory.

    ``post`` delivers it, together with any further messages of the same batch it is given, ``serve_file`` answers the
    file download and ``calls`` records every media request.
    """

    accept: AsyncMock
    replay: AsyncMock
    storage: LocalStorageBackend
    serve_file: Callable[[httpx.Request], httpx.Response]
    post: Callable[..., Awaitable[httpx.Response]]
    calls: list[httpx.Request] = field(default_factory=list)


@pytest.fixture
def cloud_media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CloudMedia:
    config = channel_config("whatsapp")
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    accept = AsyncMock(return_value=AcceptedChannelEvent(replayed=False))
    replay = AsyncMock(return_value=False)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    message = {
        "id": "native-media",
        "from": "15550001",
        "type": "document",
        "document": {"id": "media1", "filename": "report.xlsx"},
    }

    async def post(*more_messages: dict[str, object]) -> httpx.Response:
        value = {"metadata": {"phone_number_id": config.app_id}, "messages": [message, *more_messages]}
        body = json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()
        headers = {"x-hub-signature-256": "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()}
        async with webhook_client(whatsapp.router) as client:
            return await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=body, headers=headers)

    media = CloudMedia(accept, replay, storage, lambda _: httpx.Response(200, content=b"file payload"), post)

    def respond(request: httpx.Request) -> httpx.Response:
        media.calls.append(request)
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media1"})
        return media.serve_file(request)

    @asynccontextmanager
    async def provider_client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            yield http

    monkeypatch.setattr(whatsapp_cloud_media, "public_http_client", provider_client)
    return media


async def test_stalled_whatsapp_file_host_is_cut_off_at_the_budget_and_the_message_is_accepted(
    cloud_media, ack_clock, monkeypatch
):
    # Given: a budget of a millisecond, and a file host that takes the request and then sends nothing
    set_ack_budget(monkeypatch, 0.001)
    cloud_media.serve_file = lambda _: httpx.Response(200, stream=StalledStream())
    # When
    response = await cloud_media.post()
    # Then: WhatsApp gets its usual answer, and the message is accepted with a note and no file
    assert (response.status_code, response.json()) == (200, {"ok": True})
    cloud_media.accept.assert_awaited_once()
    assert cloud_media.accept.await_args.args[0].attachments == ()
    assert cloud_media.accept.await_args.kwargs["content"] == deadline_note("report.xlsx")
    assert not await cloud_media.storage.list_dir(str(AGENT_ID))


async def test_slow_whatsapp_file_host_is_abandoned_at_the_budget_and_the_message_is_accepted(
    cloud_media, ack_clock, monkeypatch
):
    # Given: a 2.5 second budget, and a file the host serves at one second per chunk
    set_ack_budget(monkeypatch, 2.5)
    stream = SlowStream(ack_clock, total=10 * READ_CHUNK_BYTES, step=1.0)
    cloud_media.serve_file = lambda _: httpx.Response(200, stream=stream)
    # When
    response = await cloud_media.post()
    # Then: the read stopped at the first chunk that arrived late, and the message was accepted with a note
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert stream.consumed == 3 * READ_CHUNK_BYTES
    assert cloud_media.accept.await_args.args[0].attachments == ()
    assert cloud_media.accept.await_args.kwargs["content"] == deadline_note("report.xlsx")


async def test_a_retried_media_message_while_the_first_is_downloading_is_refused_without_a_download(
    cloud_media, claim_store
):
    # Given: a first delivery held in its file download
    gate = GatedStream()
    cloud_media.serve_file = lambda _: httpx.Response(200, stream=gate)
    first: list[httpx.Response] = []

    async def deliver() -> None:
        first.append(await cloud_media.post())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(deliver)
        await gate.started.wait()
        # When: WhatsApp redelivers the message while the first request is still working
        retry = await cloud_media.post()
        # Then: WhatsApp gets the retryable refusal, with no new download and nothing accepted yet
        assert_in_flight_refusal(retry)
        assert len(cloud_media.calls) == 2
        assert cloud_media.replay.await_count == 1
        cloud_media.accept.assert_not_awaited()
        gate.release.set()
    # And: the first delivery went on to accept the message once, then released the claim
    assert first[0].status_code == 200
    cloud_media.accept.assert_awaited_once()
    assert claim_store.claims == {}


async def test_a_batch_with_a_held_message_processes_the_rest_and_answers_the_whole_delivery_with_503(
    cloud_media, claim_store
):
    # Given: a first delivery of the media message held in its file download, and a text message that arrives with it
    gate = GatedStream()
    cloud_media.serve_file = lambda _: httpx.Response(200, stream=gate)
    text = {"id": "native-text", "from": "15550001", "type": "text", "text": {"body": "hello"}}
    first: list[httpx.Response] = []

    async def deliver() -> None:
        first.append(await cloud_media.post())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(deliver)
        await gate.started.wait()
        # When: WhatsApp redelivers the batch with the held media message and the text message
        retry = await cloud_media.post(text)
        # Then: the text message was accepted as usual, the held one was not downloaded again, and the delivery is refused
        assert_in_flight_refusal(retry)
        assert [call.args[0].event_key for call in cloud_media.accept.await_args_list] == ["native-text"]
        assert len(cloud_media.calls) == 2
        gate.release.set()
    # And: the first delivery went on to accept the media message, then released the claim
    assert first[0].status_code == 200
    assert [call.args[0].event_key for call in cloud_media.accept.await_args_list] == ["native-text", "native-media"]
    assert claim_store.claims == {}


async def test_a_media_message_is_not_lost_when_the_delivery_that_held_its_claim_fails(cloud_media, claim_store):
    # Given: a first delivery held in its file download, whose acceptance will then fail
    cloud_media.accept.side_effect = [HTTPException(503, "retry"), AcceptedChannelEvent(replayed=False)]
    gate = GatedStream()
    cloud_media.serve_file = lambda _: httpx.Response(200, stream=gate)
    first: list[httpx.Response] = []

    async def deliver() -> None:
        first.append(await cloud_media.post())

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(deliver)
        await gate.started.wait()
        # And: WhatsApp's retry arrives meanwhile and is refused, so WhatsApp keeps the message pending
        assert_in_flight_refusal(await cloud_media.post())
        gate.release.set()
    assert first[0].status_code == 503
    assert claim_store.claims == {}
    # When: WhatsApp redelivers it after the first delivery failed and released its claim
    retried = await cloud_media.post()
    # Then: the redelivery is processed in full, so the message is not lost
    assert (retried.status_code, retried.json()) == (200, {"ok": True})
    assert cloud_media.accept.await_count == 2


async def test_a_whatsapp_file_fetched_as_the_deadline_passes_is_not_stored_and_the_message_is_accepted(
    cloud_media, ack_clock, monkeypatch
):
    # Given: a 2 second budget, and a file whose body ends after it
    set_ack_budget(monkeypatch, 2.0)
    cloud_media.serve_file = lambda _: httpx.Response(200, stream=ClockedStream(ack_clock, 3.0))
    # When
    response = await cloud_media.post()
    # Then: the file was fetched but not stored, and the message was accepted with a note
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert not await cloud_media.storage.list_dir(str(AGENT_ID))
    assert cloud_media.accept.await_args.args[0].attachments == ()
    assert cloud_media.accept.await_args.kwargs["content"] == deadline_note("report.xlsx")


async def test_a_whatsapp_store_that_starts_before_the_deadline_runs_to_completion_and_is_attached(
    cloud_media, ack_clock, monkeypatch
):
    # Given: a 2 second budget, and a store that starts in time and finishes after the deadline
    set_ack_budget(monkeypatch, 2.0)
    store = store_overrunning_the_deadline(ack_clock, whatsapp_cloud_media.store_agent_upload)
    monkeypatch.setattr(whatsapp_cloud_media, "store_agent_upload", store)
    # When
    response = await cloud_media.post()
    # Then: the file was stored once and attached, with no note and no other file left in the workspace
    assert (response.status_code, response.json()) == (200, {"ok": True})
    (reference,) = cloud_media.accept.await_args.args[0].attachments
    assert cloud_media.accept.await_args.kwargs["content"] == f"[file:{reference.path}]"
    assert await cloud_media.storage.read_bytes(f"{AGENT_ID}/{reference.path}") == b"file payload"
    assert len(await cloud_media.storage.list_dir(f"{AGENT_ID}/workspace/uploads")) == 1


async def test_a_conflicting_media_message_keeps_its_retryable_answer_and_releases_the_claim(cloud_media, claim_store):
    # Given: the replay check finds a different event under the same key
    cloud_media.replay.side_effect = ChannelEventConflictError(AGENT_ID, "whatsapp", "native-media")
    # When
    response = await cloud_media.post()
    # Then: WhatsApp still gets the failure it retries on, nothing was downloaded, and no claim is left
    assert response.status_code == 503
    assert cloud_media.calls == []
    assert claim_store.claims == {}


async def test_a_failed_media_message_releases_its_claim_so_the_providers_retry_is_processed(cloud_media, claim_store):
    # Given: an acceptance that fails once
    cloud_media.accept.side_effect = [HTTPException(503, "retry"), AcceptedChannelEvent(replayed=False)]
    # When
    failed = await cloud_media.post()
    assert claim_store.claims == {}
    retried = await cloud_media.post()
    # Then: WhatsApp saw the failure, and its retry was processed in full
    assert (failed.status_code, retried.status_code) == (503, 200)
    assert cloud_media.accept.await_count == 2
    assert len(cloud_media.calls) == 4


@pytest.mark.parametrize("failure", [ConnectionError("redis down"), RedisPoolOverloadError("cache pool busy")])
async def test_an_unavailable_claim_store_falls_back_to_processing_the_media_message(cloud_media, claim_store, failure):
    # Given: Redis is down, or its pool has no free connection
    claim_store.failure = failure
    # When
    response = await cloud_media.post()
    # Then: the message is processed as it was before claims existed
    assert (response.status_code, response.json()) == (200, {"ok": True})
    cloud_media.accept.assert_awaited_once()
    assert len(cloud_media.accept.await_args.args[0].attachments) == 1
