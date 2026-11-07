from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import background_tasks, feishu as feishu_api
from app.core.domain.channels import ChannelType
from app.core.json_types import JsonObject
from app.db import session as session_module
from app.records.channel_config import ChannelConfigRecord
from app.schemas.schemas import ChannelConfigCreate
from app.services.channels import binding, feishu_submissions, submissions
from app.services.channels.media_limits import (
    READ_CHUNK_BYTES,
    MediaBudget,
    MediaTooLargeError,
    too_large_note,
    too_large_reply,
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
async def test_process_feishu_event_rechecks_config_despite_redis_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=True))
    config = AsyncMock(return_value=None)
    model = AsyncMock()
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", config)
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", model)

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 1, "msg": "Channel not found"}
    config.assert_awaited_once()
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_feishu_event_missing_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-2", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 1, "msg": "Channel not found"}


@pytest.mark.asyncio
async def test_process_text_group_message_uses_chat_and_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(app_id="cli_app", app_secret="secret")
    logs: list[str] = []
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(feishu_api.channel_inbound, "load_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_api.logger, "info", lambda msg, *args, **kwargs: logs.append(str(msg)))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {
            "header": {"event_id": "evt-3", "event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_type": "text",
                    "chat_type": "group",
                    "chat_id": "oc_group",
                    "content": '{"text":"hi"}',
                },
                "sender": {"sender_id": {"open_id": "ou_open", "user_id": "ou_tenant_user"}},
            },
        },
    )

    assert result == {"code": 1, "msg": "Agent not found"}
    assert any("chat_type=group" in line and "user_id_from_event='ou_tenant_user'" in line for line in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["file", "image"])
async def test_file_webhook_acks_blocked_work_and_logs_background_failure(monkeypatch, message_type):
    # Given
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(
        feishu_api.channel_config_dao,
        "get_for_agent",
        AsyncMock(
            return_value=SimpleNamespace(
                app_id="app", app_secret="secret", verification_token="token", encrypt_key="", extra_config={}
            ),
        ),
    )
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))
    payload = json.dumps(
        {
            "schema": "2.0",
            "header": {"event_id": "file-event", "event_type": "im.message.receive_v1", "token": "token"},
            "event": {"message": {"message_type": message_type}, "sender": {"sender_id": {"open_id": "user"}}},
        }
    ).encode()
    signature = hashlib.sha256(b"123nonce" + payload).hexdigest().encode()
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-lark-request-timestamp", b"123"),
                (b"x-lark-request-nonce", b"nonce"),
                (b"x-lark-signature", signature),
            ],
        },
        AsyncMock(return_value={"type": "http.request", "body": payload}),
    )
    started, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = RuntimeError("file processing failed")
    logger = Mock()
    logger.opt.return_value.error.side_effect = lambda _: observed.set()
    monkeypatch.setattr(background_tasks, "logger", logger)

    async def process(*args):
        started.set()
        await release.wait()
        raise failure

    monkeypatch.setattr(feishu_api, "_handle_feishu_file", process)
    # When
    try:
        result = await asyncio.wait_for(feishu_api.feishu_event_webhook(AGENT_ID, request), 1)
        await asyncio.wait_for(started.wait(), 1)
        # Then
        assert result == {"code": 0, "msg": "ok"}
        assert not observed.is_set()
    finally:
        release.set()
        await asyncio.wait_for(observed.wait(), 1)
    logger.opt.assert_called_once_with(exception=failure)
    logger.opt.return_value.error.assert_called_once_with("Background task failed: Feishu handle file")


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


@pytest.fixture
def legacy_feishu(monkeypatch):
    """The Feishu handlers that run for events without a message id, with every resource request captured."""
    store = AsyncMock(return_value=("key", "workspace/uploads/image.jpg", "path"))
    send = AsyncMock(return_value={})
    config = ChannelConfigRecord(id=uuid.uuid4(), agent_id=AGENT_ID, app_id="app", app_secret="secret")
    monkeypatch.setattr(feishu_api, "store_agent_upload", store)
    monkeypatch.setattr(feishu_api.feishu_service, "send_message", send)
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(feishu_api.channel_inbound, "load_agent", AsyncMock(return_value=None))
    requested = []

    async def deliver(entry, resource, keys=(IMAGE_KEY,)):
        def serve_file(request):
            requested.append(request.url.path.rsplit("/", 1)[-1])
            return resource(request)

        serve_resource(monkeypatch, serve_file)
        if entry == "post_images":
            return await feishu_api._download_post_images(AGENT_ID, config, "message-id", list(keys))
        if entry == "file":
            message: JsonObject = {"message_type": "image", "content": json.dumps({"image_key": keys[0]})}
            return await feishu_api._handle_feishu_file(
                AGENT_ID, config, message, "ou_open", "tenant-user", "p2p", "oc_chat"
            )
        images = [{"tag": "img", "image_key": key} for key in keys]
        post = {"title": "", "content": [[{"tag": "text", "text": "hello"}, *images]]}
        post_message: JsonObject = {
            "message_type": "post",
            "chat_type": "p2p",
            "chat_id": "oc_chat",
            "content": json.dumps(post),
        }
        event: JsonObject = {
            "sender": {"sender_id": {"open_id": "ou_open", "user_id": "tenant-user"}},
            "message": post_message,
        }
        body: JsonObject = {"header": {"event_id": "evt", "event_type": "im.message.receive_v1"}, "event": event}
        return await feishu_api.process_feishu_event(AGENT_ID, body)

    return SimpleNamespace(deliver=deliver, requested=requested, store=store, send=send)


@pytest.mark.asyncio
async def test_legacy_feishu_post_spends_an_aborted_read_so_the_next_image_is_never_requested(
    legacy_feishu, monkeypatch
):
    # Given: an event allowance of three read chunks, and a post of two images
    monkeypatch.setattr(feishu_api, "MediaBudget", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
    # When
    await legacy_feishu.deliver(
        "post", lambda _: httpx.Response(200, stream=CountingStream(total=10 * READ_CHUNK_BYTES)), POST_KEYS
    )
    # Then: only the first image was requested
    assert legacy_feishu.requested == POST_KEYS[:1]
    legacy_feishu.store.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["file", "post", "post_images"])
@pytest.mark.parametrize("failure", ["status_503", "read_timeout", "storage_quota"])
async def test_legacy_feishu_media_handlers_let_transient_failures_propagate(legacy_feishu, entry, failure):
    # Given
    def resource(request):
        if failure == "read_timeout":
            raise httpx.ReadTimeout("slow provider", request=request)
        return httpx.Response(503 if failure == "status_503" else 200, content=b"image-bytes")

    legacy_feishu.store.side_effect = StorageQuotaError("agent storage is full")
    expected = {"status_503": httpx.HTTPStatusError, "read_timeout": httpx.ReadTimeout}.get(failure, StorageQuotaError)
    # When / Then: the failure is not turned into the missing-permission tip
    with pytest.raises(expected):
        await legacy_feishu.deliver(entry, resource)
    legacy_feishu.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_post_image_downloads_charge_aborted_bytes_before_requesting_the_next_image(
    legacy_feishu, monkeypatch
):
    # Given: the first image exhausts the event while being rejected.
    monkeypatch.setattr(feishu_api, "MediaBudget", lambda: MediaBudget(remaining=3 * READ_CHUNK_BYTES))
    stream = CountingStream(total=10 * READ_CHUNK_BYTES)
    # When
    await legacy_feishu.deliver("post_images", lambda _: httpx.Response(200, stream=stream), POST_KEYS)
    # Then: the remaining image causes no network or storage work.
    assert legacy_feishu.requested == POST_KEYS[:1]
    assert stream.consumed == 4 * READ_CHUNK_BYTES
    legacy_feishu.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_feishu_file_handler_still_explains_a_refused_download(legacy_feishu):
    # Given: Feishu answers the download with 403
    # When
    await legacy_feishu.deliver("file", lambda _: httpx.Response(403))
    # Then: the sender is pointed at the im:resource permission and nothing was stored
    assert "im:resource" in json.loads(legacy_feishu.send.await_args_list[0].args[4])["text"]
    legacy_feishu.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_feishu_post_skips_a_refused_image_and_keeps_processing(legacy_feishu):
    # Given: Feishu answers the image download with 403
    # When
    result = await legacy_feishu.deliver("post", lambda _: httpx.Response(403))
    # Then: the message is still processed
    assert result == {"code": 1, "msg": "Agent not found"}
    legacy_feishu.store.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_feishu_file_handler_reports_oversize_instead_of_the_permission_tip(monkeypatch):
    # Given: a legacy file event without a message id whose image is over the cap
    download = AsyncMock(side_effect=MediaTooLargeError(IMAGE_UPLOAD_MAX_BYTES))
    send = AsyncMock(return_value={})
    monkeypatch.setattr(feishu_api.feishu_service, "download_message_resource", download)
    monkeypatch.setattr(feishu_api.feishu_service, "send_message", send)
    config = ChannelConfigRecord(id=uuid.uuid4(), agent_id=AGENT_ID, app_id="app", app_secret="secret")
    message: JsonObject = {"message_type": "image", "content": json.dumps({"image_key": IMAGE_KEY})}
    # When
    await feishu_api._handle_feishu_file(AGENT_ID, config, message, "ou_open", "tenant-user", "p2p", "oc_chat")
    # Then: the sender is told the size limit that applied to the download
    assert download.await_args_list[0].kwargs["max_bytes"] == IMAGE_UPLOAD_MAX_BYTES
    assert json.loads(send.await_args_list[0].args[4]) == {"text": too_large_reply(IMAGE_NAME, IMAGE_UPLOAD_MAX_BYTES)}


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
