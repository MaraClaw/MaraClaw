from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import feishu as feishu_api
from app.core.domain.channels import ChannelType
from app.db import session as session_module
from app.records.channel_config import ChannelConfigRecord
from app.schemas.schemas import ChannelConfigCreate
from app.services.channels import binding, feishu_submissions, submissions
from app.services.channels.media_limits import (
    READ_CHUNK_BYTES,
    MediaBudget,
    too_large_note,
)
from app.services.document_parser import IMAGE_UPLOAD_MAX_BYTES
from app.services.storage_runtime.quota_errors import StorageQuotaError
from channel_media_fixtures import CountingStream, serve
from im_token_cache_fakes import install_token_cache
from test_channel_shared_processors import context, pipeline
from test_db_session import _FakePool, _FakeRawConnection

__all__ = ["context", "pipeline"]

AGENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.mark.asyncio
async def test_webhook_echoes_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        feishu_api.channel_config_dao,
        "get_for_agent",
        AsyncMock(return_value=SimpleNamespace(verification_token="token", encrypt_key="", extra_config={})),
    )
    request = Request(
        {"type": "http", "headers": []},
        AsyncMock(
            return_value={
                "type": "http.request",
                "body": b'{"type":"url_verification","token":"token","challenge":"verify-me"}',
            }
        ),
    )

    result = await feishu_api.feishu_event_webhook(AGENT_ID, request)

    assert result == {"challenge": "verify-me"}


@pytest.mark.asyncio
async def test_process_feishu_event_missing_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    accept = AsyncMock()
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_submissions, "accept_feishu_event", accept)

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-2", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 1, "msg": "Channel not found"}
    accept.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("existing", "mode"), [(False, "websocket"), (True, "websocket"), (True, "webhook")])
async def test_config_connector_waits_for_save_and_commit(monkeypatch, existing, mode):
    # Given
    from app.services.feishu_ws import feishu_ws_manager

    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(feishu_api, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(feishu_api, "is_agent_creator", lambda *_: True)
    config = ChannelConfigRecord(
        id=uuid.uuid4(),
        agent_id=AGENT_ID,
        channel_type=ChannelType.FEISHU,
        app_id="app",
        app_secret="secret",
        encrypt_key="",
        verification_token="",
        is_configured=True,
        is_connected=False,
        last_tested_at=None,
        extra_config={"connection_mode": mode},
        created_at=None,
    )
    monkeypatch.setattr(
        feishu_api.channel_config_dao,
        "get_for_agent",
        AsyncMock(
            return_value=config if existing else None,
        ),
    )
    saved, started = asyncio.Event(), asyncio.Event()
    seen = []

    async def save(**kwargs):
        saved.set()
        return config

    async def connect(*args):
        seen.append((saved.is_set(), raw.commits))
        started.set()

    monkeypatch.setattr(feishu_api.channel_config_dao, "update", save)
    monkeypatch.setattr(feishu_api.channel_config_dao, "create", save)
    monkeypatch.setattr(feishu_ws_manager, "start_client" if mode == "websocket" else "stop_client", connect)
    data = ChannelConfigCreate(
        app_id="app", app_secret="secret", verification_token="token", extra_config={"connection_mode": mode}
    )
    token = session_module._conn_ctx.set(None)
    # When
    try:
        async with session_module.connection_ctx():
            result = await feishu_api.configure_channel(AGENT_ID, data, Mock())
            barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(barrier.set)
            await barrier.wait()
            assert not started.is_set()
        await asyncio.wait_for(started.wait(), 1)
    finally:
        session_module._conn_ctx.reset(token)
    # Then
    assert result.agent_id == AGENT_ID
    assert seen == [(True, 1)]


@pytest.mark.asyncio
async def test_delete_channel_stops_the_websocket_client_after_the_config_is_removed(monkeypatch):
    # Given a creator deleting an existing Feishu channel inside the request transaction.
    from app.services.feishu_ws import feishu_ws_manager

    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(feishu_api, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(feishu_api, "is_agent_creator", lambda *_: True)
    config = Mock(id=uuid.uuid4())
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    order, stopped = [], asyncio.Event()

    async def delete(**_kwargs):
        order.append(("delete", raw.commits))
        return True

    async def stop(agent_id):
        order.append(("stop", raw.commits, agent_id))
        stopped.set()

    monkeypatch.setattr(feishu_api.channel_config_dao, "delete", delete)
    monkeypatch.setattr(feishu_ws_manager, "stop_client", stop)
    token = session_module._conn_ctx.set(None)
    # When the request transaction commits.
    try:
        async with session_module.connection_ctx():
            await feishu_api.delete_channel_config(AGENT_ID, Mock())
            await asyncio.sleep(0)
            assert not stopped.is_set()
        await asyncio.wait_for(stopped.wait(), 1)
    finally:
        session_module._conn_ctx.reset(token)
    # Then the client is stopped only after the config removal committed.
    assert order == [("delete", 0), ("stop", 1, AGENT_ID)]


IMAGE_KEY = "img_v3_0123456789"
IMAGE_NAME = f"image_{IMAGE_KEY[-8:]}.jpg"
POST_KEYS = ["img_key_00000000", "img_key_00000001"]


def feishu_event(kind, content):
    return {
        "sender": {"sender_id": {"open_id": "ou_open", "user_id": "tenant-user"}},
        "message": {
            "message_id": "om_native",
            "message_type": kind,
            "chat_type": "p2p",
            "chat_id": "oc_chat",
            "content": json.dumps(content),
        },
    }


def serve_resource(monkeypatch, resource):
    _ = install_token_cache(monkeypatch)

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"app_access_token": "token"})
        return resource(request)

    serve(monkeypatch, handler)


@pytest.fixture
def feishu_acceptance(pipeline, monkeypatch):
    config = pipeline[4]
    config.channel_type, config.app_id, config.app_secret = "feishu", "app-id", "app-secret"
    upload = AsyncMock(return_value=("key", "workspace/uploads/image.jpg", "path"))
    monkeypatch.setattr(feishu_submissions, "store_agent_upload", upload)
    return config, upload, pipeline[6], pipeline[7]


@pytest.mark.asyncio
async def test_oversize_feishu_image_is_skipped_with_a_note_and_the_event_is_accepted(feishu_acceptance, monkeypatch):
    # Given: a Feishu image far past the 10 MiB image cap, served without Content-Length
    config, upload, persist, enqueue = feishu_acceptance
    stream = CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES)
    serve_resource(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    # When
    accepted = await feishu_submissions.accept_feishu_event(config, feishu_event("image", {"image_key": IMAGE_KEY}))
    # Then: the read stopped at the cap, nothing was stored or embedded, and the transcript carries the note
    note = too_large_note(IMAGE_NAME, IMAGE_UPLOAD_MAX_BYTES)
    assert accepted
    assert IMAGE_UPLOAD_MAX_BYTES < stream.consumed <= IMAGE_UPLOAD_MAX_BYTES + READ_CHUNK_BYTES
    upload.assert_not_awaited()
    assert persist.await_args.kwargs["content"] == note
    assert enqueue.await_args.args[0].content == note


@pytest.mark.asyncio
async def test_feishu_post_images_share_one_event_budget(feishu_acceptance, monkeypatch):
    # Given: a post of four 1 MiB images and an event allowance of 2.5 MiB
    config, upload, persist, enqueue = feishu_acceptance
    monkeypatch.setattr(feishu_submissions, "MediaBudget", lambda: MediaBudget(remaining=5 * 1024 * 1024 // 2))
    serve_resource(monkeypatch, lambda _: httpx.Response(200, content=b"x" * 1024 * 1024))
    keys = [f"img_key_0000000{index}" for index in range(4)]
    post = {"title": "", "content": [[{"tag": "img", "image_key": key} for key in keys]]}
    # When
    await feishu_submissions.accept_feishu_event(config, feishu_event("post", post))
    # Then: two images fit; the other two are skipped against what is left of the event
    assert upload.await_count == 2
    left = 512 * 1024
    notes = [too_large_note(f"image_{key[-8:]}.jpg", left) for key in keys[2:]]
    assert persist.await_args.kwargs["content"].splitlines()[-2:] == notes
    assert enqueue.await_args.args[0].content.count("[image_data:") == 2


@pytest.mark.asyncio
async def test_redelivered_oversize_feishu_event_replays_without_a_second_download(feishu_acceptance, monkeypatch):
    # Given: an oversize event that was accepted once
    config, upload, persist, _ = feishu_acceptance
    downloads = []

    def resource(request):
        downloads.append(request.url)
        return httpx.Response(200, stream=CountingStream(total=3 * IMAGE_UPLOAD_MAX_BYTES))

    serve_resource(monkeypatch, resource)
    event = feishu_event("image", {"image_key": IMAGE_KEY})
    await feishu_submissions.accept_feishu_event(config, event)
    receipts = submissions.channel_event_receipt_dao
    assert isinstance(receipts.find, AsyncMock)
    assert isinstance(receipts.reserve, AsyncMock)
    assert receipts.reserve.await_args is not None
    receipts.find.return_value = receipts.reserve.await_args.args[0]
    # When: the provider delivers the same event again
    assert await feishu_submissions.accept_feishu_event(config, event)
    # Then: the receipt answers it, with no new download, storage write or transcript row
    assert len(downloads) == 1
    receipts.reserve.assert_awaited_once()
    persist.assert_awaited_once()
    upload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status_503", "read_timeout", "storage_quota"])
async def test_feishu_transient_failures_still_propagate_without_accepting(feishu_acceptance, monkeypatch, failure):
    # Given
    config, upload, persist, _ = feishu_acceptance

    def resource(request):
        if failure == "read_timeout":
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(503 if failure == "status_503" else 200, content=b"image-bytes")

    serve_resource(monkeypatch, resource)
    upload.side_effect = StorageQuotaError("agent storage is full")
    expected = {"status_503": httpx.HTTPStatusError, "read_timeout": httpx.ReadTimeout}.get(failure, StorageQuotaError)
    # When / Then: the provider keeps its retry signal and no receipt is reserved
    with pytest.raises(expected):
        await feishu_submissions.accept_feishu_event(config, feishu_event("image", {"image_key": IMAGE_KEY}))
    assert isinstance(submissions.channel_event_receipt_dao.reserve, AsyncMock)
    submissions.channel_event_receipt_dao.reserve.assert_not_awaited()
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_aborted_oversize_feishu_read_spends_the_event_so_the_next_image_is_never_requested(
    feishu_acceptance, monkeypatch
):
    # Given: an event allowance of three read chunks, and a post of two images
    config, upload, persist, _ = feishu_acceptance
    monkeypatch.setattr(feishu_submissions, "MediaBudget", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
    requested = []

    def resource(request):
        requested.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, stream=CountingStream(total=10 * READ_CHUNK_BYTES))

    serve_resource(monkeypatch, resource)
    post = {"title": "", "content": [[{"tag": "img", "image_key": key} for key in POST_KEYS]]}
    # When
    await feishu_submissions.accept_feishu_event(config, feishu_event("post", post))
    # Then: the bytes pulled from the first image used up the event, so the second is skipped without a request
    assert requested == POST_KEYS[:1]
    upload.assert_not_awaited()
    notes = [too_large_note(f"image_{POST_KEYS[0][-8:]}.jpg", 3 * READ_CHUNK_BYTES)]
    notes.append(too_large_note(f"image_{POST_KEYS[1][-8:]}.jpg", 0))
    assert persist.await_args.kwargs["content"].splitlines() == notes


@pytest.mark.asyncio
async def test_unauthorized_feishu_event_is_refused_before_any_download(feishu_acceptance, monkeypatch):
    # Given: a sender who no longer has access to the agent
    config, upload, persist, _ = feishu_acceptance
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"image-bytes")

    serve(monkeypatch, handler)
    monkeypatch.setattr(binding, "check_agent_access", AsyncMock(side_effect=HTTPException(403)))
    # When / Then: authorization fails before the provider is contacted
    with pytest.raises(HTTPException):
        await feishu_submissions.accept_feishu_event(config, feishu_event("image", {"image_key": IMAGE_KEY}))
    assert requests == []
    upload.assert_not_awaited()
    persist.assert_not_awaited()
