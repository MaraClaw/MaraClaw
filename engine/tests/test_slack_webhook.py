import hashlib
import hmac
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import HTTPException
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import slack
from app.config import get_settings
from app.core.events import RedisPoolOverloadError
from app.services.channels import binding, slack_submissions, submissions
from app.services.channels.media_limits import (
    READ_CHUNK_BYTES,
    MediaBudget,
    deadline_note,
    skipped_attachments_note,
    too_large_note,
    too_large_reply,
)
from app.services.document_parser import IMAGE_UPLOAD_MAX_BYTES
from app.services.storage_runtime.quota_errors import StorageQuotaError
from channel_media_fixtures import (
    ClockedStream,
    CountingStream,
    GatedStream,
    SlowStream,
    StalledStream,
    ack_clock,
    assert_in_flight_refusal,
    claim_store,
    serve,
    set_ack_budget,
    set_attachment_limit,
    store_overrunning_the_deadline,
)
from channel_shared_webhook_fixtures import context, pipeline, request_for, webhook_pipeline

__all__ = ["ack_clock", "claim_store", "context", "pipeline", "webhook_pipeline"]

NOW = 1_700_000_000
SECRET = "app-secret"


@pytest.mark.parametrize(
    "failure",
    [None, "missing", "forged", "non_ascii", "timestamp", "huge_timestamp", "stale", "utf8", "json"],
)
async def test_slack_malformed_auth_rejects_before_acceptance(monkeypatch, failure):
    # Given: a real signed Slack challenge or a deliberately malformed auth input.
    body = b'{"type":"url_verification","challenge":"challenge"}'
    if failure == "utf8":
        body = b"\xff"
    if failure == "json":
        body = b"invalid json"
    timestamp = str(NOW)
    if failure == "timestamp":
        timestamp = "not-a-number"
    if failure == "huge_timestamp":
        timestamp = "9" * 400
    if failure == "stale":
        timestamp = str(NOW - 301)
    signature = (
        b"v0="
        + hmac.new(SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest().encode()
    )
    if failure == "forged":
        signature = b"v0=forged"
    if failure == "non_ascii":
        signature = b"\xff"
    headers = [(b"x-slack-request-timestamp", timestamp.encode()), (b"x-slack-signature", signature)]
    if failure == "missing":
        headers = []
    monkeypatch.setattr(slack.time, "time", lambda: NOW)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("slack")))
    accept = AsyncMock()
    monkeypatch.setattr(slack_submissions, "accept_slack_event", accept)

    # When: traversing the real webhook route with raw header bytes.
    async with webhook_client(slack.router) as client:
        response = await client.post(f"/api/channel/slack/{AGENT_ID}/webhook", content=body, headers=headers)

    # Then: malformed authentication never crashes or accepts a turn.
    assert response.status_code == (200 if failure is None else 401)
    if failure is None:
        assert response.json() == {"challenge": "challenge"}
    accept.assert_not_called()


async def test_slack_exact_cap_valid_signature_preserves_challenge(monkeypatch):
    # Given: a correctly signed challenge padded to the channel byte budget.
    body = b'{"type":"url_verification","challenge":"boundary"}'
    body += b" " * (1024 * 1024 - len(body))
    timestamp = str(NOW)
    digest = hmac.new(SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(slack.time, "time", lambda: NOW)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=channel_config("slack")))

    # When: the actual webhook authenticates all bytes, including boundary padding.
    async with webhook_client(slack.router) as client:
        response = await client.post(
            f"/api/channel/slack/{AGENT_ID}/webhook",
            content=body,
            headers={"x-slack-request-timestamp": timestamp, "x-slack-signature": f"v0={digest}"},
        )

    # Then: an exact-cap authentic body is not mistaken for overflow.
    assert response.status_code == 200
    assert response.json() == {"challenge": "boundary"}


OVERSIZE_IMAGE = {"id": "F1", "name": "photo.png", "url_private_download": "https://files.slack.com/photo.png"}
BIG_THEN_SMALL = [
    {"id": "F1", "name": "big.bin", "url_private_download": "https://files.slack.com/big.bin"},
    {"id": "F2", "name": "small.txt", "url_private_download": "https://files.slack.com/small.txt"},
]
OVERSIZE_THEN_TWO_SMALL = [
    OVERSIZE_IMAGE,
    {"id": "F2", "name": "small.txt", "url_private_download": "https://files.slack.com/small.txt"},
    {"id": "F3", "name": "extra.txt", "url_private_download": "https://files.slack.com/extra.txt"},
]


def slack_body(files, *, ts="1700000000.000100"):
    event = {"type": "message", "user": "U123", "channel": "D123", "text": "see attached", "files": files}
    if ts:
        event["ts"] = ts
    return {"type": "event_callback", "team_id": "T1", "event": event}


@pytest.fixture
def slack_acceptance(pipeline, monkeypatch):
    config = pipeline[4]
    config.channel_type, config.app_secret = "slack", "xoxb-token"
    upload = AsyncMock(return_value=("key", "workspace/uploads/photo.png", "path"))
    monkeypatch.setattr(slack_submissions, "store_agent_upload", upload)
    return config, upload, pipeline[6], pipeline[7]


async def test_oversize_slack_file_is_skipped_with_a_note_and_the_event_is_accepted(slack_acceptance, monkeypatch):
    # Given: a Slack image far past the 10 MiB image cap, served without Content-Length
    config, upload, persist, enqueue = slack_acceptance
    stream = CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES)
    serve(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    # When
    accepted = await slack_submissions.accept_slack_event(config, slack_body([OVERSIZE_IMAGE]))
    # Then: the read stopped at the cap, nothing was stored, and the transcript carries the skipped-file note
    note = too_large_note("photo.png", IMAGE_UPLOAD_MAX_BYTES)
    assert accepted
    assert IMAGE_UPLOAD_MAX_BYTES < stream.consumed <= IMAGE_UPLOAD_MAX_BYTES + READ_CHUNK_BYTES
    upload.assert_not_awaited()
    assert persist.await_args.kwargs["content"] == f"see attached\n{note}"
    assert enqueue.await_args.args[0].content == f"see attached\n{note}"


async def test_redelivered_oversize_slack_event_replays_without_a_second_download(slack_acceptance, monkeypatch):
    # Given: an oversize event that was accepted once
    config, upload, persist, _ = slack_acceptance
    downloads = []

    def handler(request):
        downloads.append(request.url)
        return httpx.Response(200, stream=CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES))

    serve(monkeypatch, handler)
    body = slack_body([OVERSIZE_IMAGE])
    await slack_submissions.accept_slack_event(config, body)
    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    receipts.find.return_value = receipts.reserve.await_args_list[0].args[0]
    # When: the provider delivers the same event again
    assert await slack_submissions.accept_slack_event(config, body)
    # Then: the receipt answers it, with no new download, storage write or transcript row
    assert len(downloads) == 1
    receipts.reserve.assert_awaited_once()
    persist.assert_awaited_once()
    upload.assert_not_awaited()


@pytest.mark.parametrize("failure", ["status_503", "read_timeout", "storage_quota"])
async def test_slack_transient_failures_still_propagate_without_accepting(slack_acceptance, monkeypatch, failure):
    # Given
    config, upload, persist, _ = slack_acceptance

    def handler(request):
        if failure == "read_timeout":
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(503 if failure == "status_503" else 200, content=b"file-bytes")

    serve(monkeypatch, handler)
    upload.side_effect = StorageQuotaError("agent storage is full")
    expected = {"status_503": httpx.HTTPStatusError, "read_timeout": httpx.ReadTimeout}.get(failure, StorageQuotaError)
    # When / Then: the provider keeps its retry signal and no receipt is reserved
    with pytest.raises(expected):
        await slack_submissions.accept_slack_event(config, slack_body([OVERSIZE_IMAGE]))
    assert isinstance(submissions.channel_event_receipt_dao.reserve, AsyncMock)
    submissions.channel_event_receipt_dao.reserve.assert_not_awaited()
    persist.assert_not_awaited()


async def test_aborted_oversize_slack_read_spends_the_event_so_the_next_file_is_never_requested(
    slack_acceptance, monkeypatch
):
    # Given: an event allowance of three read chunks, and a large file followed by a small one
    config, upload, persist, _ = slack_acceptance
    monkeypatch.setattr(MediaBudget, "for_acknowledgement", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, stream=CountingStream(total=10 * READ_CHUNK_BYTES))

    serve(monkeypatch, handler)
    # When
    await slack_submissions.accept_slack_event(config, slack_body(BIG_THEN_SMALL))
    # Then: the bytes pulled from the first file used up the event, so the second is skipped without a request
    assert requested == ["/big.bin"]
    upload.assert_not_awaited()
    notes = [too_large_note("big.bin", 3 * READ_CHUNK_BYTES), too_large_note("small.txt", 0)]
    assert persist.await_args.kwargs["content"] == "\n".join(["see attached", *notes])


@pytest.fixture
def legacy_slack(webhook_pipeline, monkeypatch):
    """The no-ts fallback of the Slack webhook, with every file request and posted reply captured."""
    pipeline, _ = webhook_pipeline
    agent, _, _, _, config, *_ = pipeline
    config.channel_type, config.app_secret = "slack", "xoxb-token"
    session = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(slack, "find_or_create_channel_session", AsyncMock(return_value=session))
    monkeypatch.setattr(
        slack.channel_inbound,
        "resolve_sender_user",
        AsyncMock(return_value=SimpleNamespace(id=uuid4(), display_name="Human")),
    )
    monkeypatch.setattr(
        slack.channel_inbound,
        "bind_channel_conversation",
        AsyncMock(return_value=SimpleNamespace(transcript_session=session, reply_session=session)),
    )
    persist = AsyncMock()
    monkeypatch.setattr(slack.channel_inbound, "persist_user_message", persist)
    assert isinstance(slack.store_agent_upload, AsyncMock)
    posted, requested = [], []

    async def deliver(files, serve_file=None):
        def handler(request):
            if request.url.host == "files.slack.com" and serve_file is not None:
                requested.append(request.url.path)
                return serve_file(request)
            if request.url.path.endswith("chat.postMessage"):
                posted.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True})

        if serve_file is not None:
            serve(monkeypatch, handler)
        payload = slack_body(files, ts="")
        payload["event"]["text"] = ""
        stamp = str(int(time.time()))
        signature = hmac.new(
            b"signing-secret", b"v0:" + stamp.encode() + b":" + json.dumps(payload).encode(), hashlib.sha256
        ).hexdigest()
        await slack.slack_event_webhook(
            agent.id, request_for(payload, {"x-slack-request-timestamp": stamp, "x-slack-signature": f"v0={signature}"})
        )

    return SimpleNamespace(
        deliver=deliver, posted=posted, requested=requested, upload=slack.store_agent_upload, persist=persist
    )


async def test_legacy_slack_fallback_tells_the_sender_the_file_was_too_large(legacy_slack):
    # Given: an event with no ts or event_id, so the legacy fallback handles it, carrying only an oversize image
    stream = CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES)
    # When
    await legacy_slack.deliver([OVERSIZE_IMAGE], lambda _: httpx.Response(200, stream=stream))
    # Then: the sender gets the size message, not the missing-permission text, and nothing was stored
    assert legacy_slack.posted == [too_large_reply("photo.png", IMAGE_UPLOAD_MAX_BYTES)]
    assert IMAGE_UPLOAD_MAX_BYTES < stream.consumed <= IMAGE_UPLOAD_MAX_BYTES + READ_CHUNK_BYTES
    legacy_slack.upload.assert_not_awaited()


async def test_legacy_slack_fallback_spends_an_aborted_read_so_the_next_file_is_never_requested(
    legacy_slack, monkeypatch
):
    # Given: an event allowance of three read chunks, and a large file followed by a small one
    monkeypatch.setattr(slack, "MediaBudget", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
    # When
    await legacy_slack.deliver(
        BIG_THEN_SMALL, lambda _: httpx.Response(200, stream=CountingStream(10 * READ_CHUNK_BYTES))
    )
    # Then: only the first file was requested, and the sender hears about both
    assert legacy_slack.requested == ["/big.bin"]
    assert legacy_slack.posted == [
        f"{too_large_reply('big.bin', 3 * READ_CHUNK_BYTES)} {too_large_reply('small.txt', 0)}"
    ]


async def test_legacy_slack_fallback_applies_the_attachment_limit_before_any_request(legacy_slack, monkeypatch):
    # Given: a limit of two and a ts-less event whose oversize first file is followed by two small ones
    set_attachment_limit(monkeypatch, 2)

    def serve_file(request):
        if request.url.path == "/photo.png":
            return httpx.Response(200, stream=CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES))
        return httpx.Response(200, content=b"small")

    # When
    await legacy_slack.deliver(OVERSIZE_THEN_TWO_SMALL, serve_file)
    # Then: the oversize file kept its slot, the third file was never requested, and the transcript has one notice
    assert legacy_slack.requested == ["/photo.png", "/small.txt"]
    legacy_slack.upload.assert_awaited_once()
    expected = ["[file:upload.bin]", too_large_note("photo.png", IMAGE_UPLOAD_MAX_BYTES), skipped_attachments_note(1)]
    assert legacy_slack.persist.await_args.kwargs["content"] == " ".join(expected)
    assert len(legacy_slack.posted) == 1


async def test_legacy_slack_fallback_ack_names_only_the_retrieved_files_and_notes_the_skipped_one(
    legacy_slack, monkeypatch
):
    # Given: a limit of two and a ts-less event of three files that Slack refuses to serve
    set_attachment_limit(monkeypatch, 2)
    # When
    await legacy_slack.deliver(OVERSIZE_THEN_TWO_SMALL, lambda _: httpx.Response(403))
    # Then: only the first two were requested, and the sender's one reply covers them plus a single skipped notice
    assert legacy_slack.requested == ["/photo.png", "/small.txt"]
    (ack,) = legacy_slack.posted
    assert "photo.png, small.txt" in ack
    assert "extra.txt" not in ack
    assert ack.count(skipped_attachments_note(1)) == 1
    legacy_slack.upload.assert_not_awaited()


@pytest.mark.parametrize("failure", ["status_503", "read_timeout", "storage_quota"])
async def test_legacy_slack_fallback_lets_transient_failures_propagate(legacy_slack, failure):
    # Given
    def serve_file(request):
        if failure == "read_timeout":
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(503 if failure == "status_503" else 200, content=b"file-bytes")

    legacy_slack.upload.side_effect = StorageQuotaError("agent storage is full")
    expected = {"status_503": httpx.HTTPStatusError, "read_timeout": httpx.ReadTimeout}.get(failure, StorageQuotaError)
    # When / Then: Slack keeps its retry signal and the sender is not told the app lacks a permission
    with pytest.raises(expected):
        await legacy_slack.deliver([OVERSIZE_IMAGE], serve_file)
    assert legacy_slack.posted == []


async def test_legacy_slack_fallback_still_explains_a_refused_download(legacy_slack):
    # Given: Slack answers the download with 403
    # When
    await legacy_slack.deliver([OVERSIZE_IMAGE], lambda _: httpx.Response(403))
    # Then: the sender is pointed at the files:read permission and nothing was stored
    assert len(legacy_slack.posted) == 1
    assert "files:read" in legacy_slack.posted[0]
    legacy_slack.upload.assert_not_awaited()


async def test_unauthorized_slack_event_is_refused_before_any_download(slack_acceptance, monkeypatch):
    # Given: a sender who no longer has access to the agent
    config, upload, persist, _ = slack_acceptance
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"file-bytes")

    serve(monkeypatch, handler)
    monkeypatch.setattr(binding, "check_agent_access", AsyncMock(side_effect=HTTPException(403)))
    # When / Then: authorization fails before the provider is contacted
    with pytest.raises(HTTPException):
        await slack_submissions.accept_slack_event(config, slack_body([OVERSIZE_IMAGE]))
    assert requests == []
    upload.assert_not_awaited()
    persist.assert_not_awaited()


SMALL_FILES = [
    {"id": f"F{n}", "name": f"file{n}.txt", "url_private_download": f"https://files.slack.com/file{n}.txt"}
    for n in (1, 2, 3)
]


async def test_stalled_slack_host_is_cut_off_at_the_budget_and_every_file_left_out_gets_a_note(
    slack_acceptance, ack_clock, monkeypatch
):
    # Given: a budget of a millisecond, and a file host that takes the request and then sends nothing
    config, upload, persist, enqueue = slack_acceptance
    set_ack_budget(monkeypatch, 0.001)
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, stream=StalledStream())

    serve(monkeypatch, handler)
    # When
    assert await slack_submissions.accept_slack_event(config, slack_body(SMALL_FILES))
    # Then: the handler returned with the event accepted, the later files were never requested, and each has a note
    assert requested == ["/file1.txt"]
    upload.assert_not_awaited()
    expected = "\n".join(["see attached", *(deadline_note(f"file{n}.txt") for n in (1, 2, 3))])
    assert persist.await_args.kwargs["content"] == expected
    assert enqueue.await_args.args[0].content == expected


async def test_slow_slack_host_is_abandoned_at_the_budget_and_the_files_it_never_served_get_notes(
    slack_acceptance, ack_clock, monkeypatch
):
    # Given: a 2.5 second budget, and a first file the host serves at one second per chunk
    config, upload, persist, _ = slack_acceptance
    set_ack_budget(monkeypatch, 2.5)
    stream = SlowStream(ack_clock, total=10 * READ_CHUNK_BYTES, step=1.0)
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, stream=stream)

    serve(monkeypatch, handler)
    # When
    assert await slack_submissions.accept_slack_event(config, slack_body(BIG_THEN_SMALL))
    # Then: the read stopped at the first chunk that arrived late, and the second file was never requested
    assert stream.consumed == 3 * READ_CHUNK_BYTES
    assert requested == ["/big.bin"]
    upload.assert_not_awaited()
    expected = "\n".join(["see attached", deadline_note("big.bin"), deadline_note("small.txt")])
    assert persist.await_args.kwargs["content"] == expected


async def test_slack_files_fetched_before_the_deadline_are_kept_and_the_rest_are_noted(
    slack_acceptance, ack_clock, monkeypatch
):
    # Given: a limit of two files, and a first store that takes three seconds of a two second budget
    config, upload, persist, _ = slack_acceptance
    set_attachment_limit(monkeypatch, 2)

    async def slow_store(agent_id, filename, data, content_type=None):
        ack_clock.advance(3.0)
        return "key", f"workspace/uploads/{filename}", "path"

    upload.side_effect = slow_store
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, content=b"small")

    serve(monkeypatch, handler)
    # When
    assert await slack_submissions.accept_slack_event(config, slack_body(SMALL_FILES))
    # Then: the first file was fetched and kept, the second was never requested, and the third is beyond the limit
    assert requested == ["/file1.txt"]
    upload.assert_awaited_once()
    assert [ref.path for ref in persist.await_args.kwargs["attachments"]] == ["workspace/uploads/file1.txt"]
    expected = ["see attached", "[file:workspace/uploads/file1.txt]", deadline_note("file2.txt")]
    assert persist.await_args.kwargs["content"] == "\n".join([*expected, skipped_attachments_note(1)])


async def test_a_retry_while_the_first_delivery_is_downloading_is_refused_without_a_second_download(
    slack_acceptance, claim_store, monkeypatch
):
    # Given: a first delivery held in its file download
    config, upload, persist, _ = slack_acceptance
    gate = GatedStream()
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, stream=gate)

    serve(monkeypatch, handler)
    body = slack_body([OVERSIZE_IMAGE])
    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.reserve, AsyncMock)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(slack_submissions.accept_slack_event, config, body)
        await gate.started.wait()
        # When: Slack redelivers the event while the first request is still working
        with pytest.raises(submissions.ChannelEventInFlightError):
            await slack_submissions.accept_slack_event(config, body)
        # Then: it is refused rather than acknowledged, with no new download, no storage write and no receipt reservation
        assert requested == ["/photo.png"]
        upload.assert_not_awaited()
        receipts.reserve.assert_not_awaited()
        gate.release.set()
    # And: the first delivery went on to store the file and accept the event once, then released the claim
    upload.assert_awaited_once()
    persist.assert_awaited_once()
    assert claim_store.claims == {}


async def test_a_failed_delivery_releases_its_claim_so_the_providers_retry_is_processed(
    slack_acceptance, claim_store, monkeypatch
):
    # Given: a delivery that fails while storing the file
    config, upload, persist, _ = slack_acceptance
    upload.side_effect = [StorageQuotaError("agent storage is full"), ("key", "workspace/uploads/photo.png", "path")]
    serve(monkeypatch, lambda _: httpx.Response(200, content=b"file-bytes"))
    body = slack_body([OVERSIZE_IMAGE])
    with pytest.raises(StorageQuotaError):
        await slack_submissions.accept_slack_event(config, body)
    # When: the provider retries it
    assert claim_store.claims == {}
    assert await slack_submissions.accept_slack_event(config, body)
    # Then: the retry was processed
    assert upload.await_count == 2
    persist.assert_awaited_once()


async def test_a_claim_left_by_a_dead_process_expires_so_a_later_retry_is_processed(
    slack_acceptance, claim_store, monkeypatch
):
    # Given: a first delivery that died after taking the claim, so its release never arrived
    config, upload, persist, _ = slack_acceptance
    claim_store.lose_releases = True
    upload.side_effect = [RuntimeError("process died"), ("key", "workspace/uploads/photo.png", "path")]
    serve(monkeypatch, lambda _: httpx.Response(200, content=b"file-bytes"))
    body = slack_body([OVERSIZE_IMAGE])
    with pytest.raises(RuntimeError, match="process died"):
        await slack_submissions.accept_slack_event(config, body)
    (key,) = claim_store.claims
    budget = get_settings().CHANNEL_MEDIA_ACK_BUDGET_SECONDS
    assert budget < claim_store.lifetimes[key] <= budget + 30
    # When: a retry arrives while the claim is still alive
    with pytest.raises(submissions.ChannelEventInFlightError):
        await slack_submissions.accept_slack_event(config, body)
    # Then: it is refused and nothing is fetched or stored again
    assert upload.await_count == 1
    persist.assert_not_awaited()
    # When: the claim's lifetime has passed
    claim_store.advance(claim_store.lifetimes[key])
    # Then: the next retry is processed
    assert await slack_submissions.accept_slack_event(config, body)
    assert upload.await_count == 2
    persist.assert_awaited_once()


async def test_an_event_is_not_lost_when_the_delivery_that_held_its_claim_fails(
    slack_acceptance, claim_store, monkeypatch
):
    # Given: a first delivery held in its file download, which will then fail to store the file
    config, upload, persist, _ = slack_acceptance
    upload.side_effect = [StorageQuotaError("agent storage is full"), ("key", "workspace/uploads/photo.png", "path")]
    gate = GatedStream()
    serve(monkeypatch, lambda _: httpx.Response(200, stream=gate))
    body = slack_body([OVERSIZE_IMAGE])

    async def first_delivery():
        with pytest.raises(StorageQuotaError):
            await slack_submissions.accept_slack_event(config, body)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(first_delivery)
        await gate.started.wait()
        # And: Slack's retry arrives meanwhile and is refused, so Slack keeps the event pending
        with pytest.raises(submissions.ChannelEventInFlightError):
            await slack_submissions.accept_slack_event(config, body)
        gate.release.set()
    # When: Slack redelivers it after the first delivery failed and released its claim
    assert claim_store.claims == {}
    assert await slack_submissions.accept_slack_event(config, body)
    # Then: the redelivery is processed in full, so the event is not lost
    assert upload.await_count == 2
    persist.assert_awaited_once()


async def test_slacks_final_retry_proceeds_despite_a_held_claim_and_the_receipt_dedupes_it(
    slack_acceptance, claim_store, monkeypatch
):
    # Given: a first delivery held in its file download, and a receipt store that reserves an event only once
    config, upload, persist, _ = slack_acceptance
    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.reserve, AsyncMock)
    assert isinstance(receipts.find, AsyncMock)
    reserved = []

    async def reserve(candidate):
        reserved.append(candidate)
        return len(reserved) == 1

    receipts.reserve.side_effect = reserve
    receipts.find.side_effect = lambda _: reserved[0] if reserved else None
    gate = GatedStream()
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, stream=gate) if len(requested) == 1 else httpx.Response(200, content=b"file-bytes")

    serve(monkeypatch, handler)
    body = slack_body([OVERSIZE_IMAGE])
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(slack_submissions.accept_slack_event, config, body)
        await gate.started.wait()
        # When: Slack's last redelivery arrives while the first request still holds the claim
        assert await slack_submissions.accept_slack_event(config, body, final_retry=True)
        # Then: it proceeds without the claim and the event is accepted once
        persist.assert_awaited_once()
        gate.release.set()
    # And: the first delivery finishing afterwards is answered by the receipt, at the cost of one repeated download
    assert requested == ["/photo.png", "/photo.png"]
    assert upload.await_count == 2
    assert len(reserved) == 2
    persist.assert_awaited_once()
    assert claim_store.claims == {}


async def test_a_slack_file_fetched_as_the_deadline_passes_is_not_stored_and_every_file_gets_a_note(
    slack_acceptance, ack_clock, monkeypatch
):
    # Given: a 2 second budget, and a first file whose body ends after it
    config, upload, persist, _ = slack_acceptance
    set_ack_budget(monkeypatch, 2.0)
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, stream=ClockedStream(ack_clock, 3.0))

    serve(monkeypatch, handler)
    # When
    assert await slack_submissions.accept_slack_event(config, slack_body(SMALL_FILES))
    # Then: the file was fetched but not stored, the others were never requested, and each has a note
    assert requested == ["/file1.txt"]
    upload.assert_not_awaited()
    assert persist.await_args.kwargs["attachments"] == ()
    expected = "\n".join(["see attached", *(deadline_note(f"file{n}.txt") for n in (1, 2, 3))])
    assert persist.await_args.kwargs["content"] == expected


async def test_a_slack_store_that_starts_before_the_deadline_runs_to_completion_and_is_attached(
    slack_acceptance, ack_clock, monkeypatch
):
    # Given: a 2 second budget, and a store that starts in time and finishes after the deadline
    config, upload, persist, _ = slack_acceptance
    set_ack_budget(monkeypatch, 2.0)

    async def store(agent_id, filename, data, content_type=None):
        return "key", f"workspace/uploads/{filename}", None

    upload.side_effect = store_overrunning_the_deadline(ack_clock, store)
    requested = []

    def handler(request):
        requested.append(request.url.path)
        return httpx.Response(200, content=b"small")

    serve(monkeypatch, handler)
    # When
    assert await slack_submissions.accept_slack_event(config, slack_body(SMALL_FILES[:1]))
    # Then: the file was stored once and attached, with no note
    assert requested == ["/file1.txt"]
    upload.assert_awaited_once()
    assert [ref.path for ref in persist.await_args.kwargs["attachments"]] == ["workspace/uploads/file1.txt"]
    assert persist.await_args.kwargs["content"] == "see attached\n[file:workspace/uploads/file1.txt]"


async def post_signed_slack_event(monkeypatch, config, *, retry_num=None):
    body = json.dumps(slack_body([OVERSIZE_IMAGE])).encode()
    timestamp = str(NOW)
    digest = hmac.new(SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": timestamp, "x-slack-signature": f"v0={digest}"}
    if retry_num is not None:
        headers["x-slack-retry-num"] = retry_num
    monkeypatch.setattr(slack.time, "time", lambda: NOW)
    monkeypatch.setattr(slack.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    async with webhook_client(slack.router) as client:
        return await client.post(f"/api/channel/slack/{AGENT_ID}/webhook", content=body, headers=headers)


async def test_slack_answers_a_held_claim_with_a_retryable_503(claim_store, monkeypatch):
    # Given: a first delivery of the event still holding its claim
    config = channel_config("slack")
    event_key = json.dumps(["T1", "D123", "1700000000.000100"], separators=(",", ":"))
    held = submissions.ChannelEvent(config.id, AGENT_ID, "slack", event_key, "U123", "slack_dm_U123", "see attached")
    async with submissions.claim_channel_event(held):
        # When: Slack redelivers the signed event
        response = await post_signed_slack_event(monkeypatch, config, retry_num="1")
    # Then: it gets the retryable refusal, not an acknowledgement
    assert_in_flight_refusal(response)


@pytest.mark.parametrize(("retry_num", "final"), [(None, False), ("1", False), ("2", False), ("3", True)])
async def test_only_slacks_third_retry_is_the_final_delivery(monkeypatch, retry_num, final):
    # Given: the event callback handler, which records how it was called
    accept = AsyncMock(return_value=True)
    monkeypatch.setattr(slack_submissions, "accept_slack_event", accept)
    # When: the signed event arrives with the given X-Slack-Retry-Num
    response = await post_signed_slack_event(monkeypatch, channel_config("slack"), retry_num=retry_num)
    # Then: the final flag is set for the third retry only
    assert response.status_code == 200
    accept.assert_awaited_once_with(ANY, ANY, final_retry=final)


@pytest.mark.parametrize("failure", [ConnectionError("redis down"), RedisPoolOverloadError("cache pool busy")])
async def test_an_unavailable_claim_store_falls_back_to_processing_the_event(
    slack_acceptance, claim_store, monkeypatch, failure
):
    # Given: Redis is down, or its pool has no free connection
    config, upload, persist, _ = slack_acceptance
    claim_store.failure = failure
    serve(monkeypatch, lambda _: httpx.Response(200, content=b"file-bytes"))
    # When / Then: the event is processed as it was before claims existed
    assert await slack_submissions.accept_slack_event(config, slack_body([OVERSIZE_IMAGE]))
    upload.assert_awaited_once()
    persist.assert_awaited_once()


async def test_the_claim_is_keyed_by_the_receipt_identity(claim_store):
    # Given: an event holding its claim
    event = submissions.ChannelEvent(uuid4(), uuid4(), "slack", "native-1", "sender", "conversation", "hello")
    async with (
        submissions.claim_channel_event(event),
        submissions.claim_channel_event(replace(event, event_key="native-2")),
        submissions.claim_channel_event(replace(event, config_id=uuid4())),
        submissions.claim_channel_event(replace(event, provider="telegram")),
    ):
        # When / Then: only the same config, provider and event key is held, whatever else the delivery carries
        with pytest.raises(submissions.ChannelEventInFlightError):
            async with submissions.claim_channel_event(replace(event, text="edited", sender_id="other")):
                pass
    assert claim_store.claims == {}
