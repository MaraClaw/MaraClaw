import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import slack
from app.services.channels import binding, slack_submissions, submissions
from app.services.channels.media_limits import READ_CHUNK_BYTES, MediaBudget, too_large_note, too_large_reply
from app.services.document_parser import IMAGE_UPLOAD_MAX_BYTES
from app.services.storage_runtime.quota_errors import StorageQuotaError
from channel_media_fixtures import CountingStream, serve
from channel_shared_webhook_fixtures import context, pipeline, request_for, webhook_pipeline

__all__ = ["context", "pipeline", "webhook_pipeline"]

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


OVERSIZE_IMAGE = {"id": "F1", "name": "photo.png", "url_private_download": "https://files.invalid/photo.png"}
BIG_THEN_SMALL = [
    {"id": "F1", "name": "big.bin", "url_private_download": "https://files.invalid/big.bin"},
    {"id": "F2", "name": "small.txt", "url_private_download": "https://files.invalid/small.txt"},
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
    monkeypatch.setattr(slack_submissions, "MediaBudget", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
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
    monkeypatch.setattr(slack.chat_message_dao, "list_recent", AsyncMock(return_value=[]))
    assert isinstance(slack.store_agent_upload, AsyncMock)
    posted, requested = [], []

    async def deliver(files, serve_file):
        def handler(request):
            if request.url.host == "files.invalid":
                requested.append(request.url.path)
                return serve_file(request)
            if request.url.path.endswith("chat.postMessage"):
                posted.append(json.loads(request.content)["text"])
            return httpx.Response(200, json={"ok": True})

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

    return SimpleNamespace(deliver=deliver, posted=posted, requested=requested, upload=slack.store_agent_upload)


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
