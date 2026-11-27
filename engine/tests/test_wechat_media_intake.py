import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException

from app.core.json_types import JsonObject
from app.core.outbound_policy import OutboundPolicyError
from app.services import wechat_channel, wechat_message_processor
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import submissions, wechat_media
from app.services.channels.media_limits import skipped_attachments_note
from app.services.storage_runtime import facade
from channel_media_fixtures import set_attachment_limit
from im_webhook_fixtures import channel_config


@pytest.fixture
def intake(monkeypatch, tmp_path):
    config = channel_config("wechat")
    config.extra_config = {"bot_token": "bot-secret"}
    monkeypatch.setattr(facade, "_storage_backend", None)
    monkeypatch.setattr(
        facade,
        "get_settings",
        lambda: SimpleNamespace(STORAGE_BACKEND="local", STORAGE_LOCAL_ROOT=str(tmp_path), AGENT_DATA_DIR="unused"),
    )
    replay, accept, context = AsyncMock(return_value=False), AsyncMock(), AsyncMock()
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    monkeypatch.setattr(wechat_channel, "remember_wechat_context", context)
    wire = SimpleNamespace(requests=[], payload=b"", status=200, headers={})

    def respond(request):
        wire.requests.append(request)
        return httpx.Response(wire.status, content=wire.payload, headers=wire.headers)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        wechat_media, "public_http_client", lambda **_: original(transport=httpx.MockTransport(respond))
    )
    return config, wire, replay, accept, context


def message(kind=4, key_encoding="raw", count=1, caption="", url=""):
    key = b"0123456789abcdef"
    key_base64 = base64.b64encode(key.hex().encode() if key_encoding == "hex" else key).decode()
    cdn: JsonObject = {"encrypt_query_param": "signed+query/secret", "aes_key": key_base64}
    if url:
        cdn["full_url"] = url
    descriptor: JsonObject = {"media": cdn}
    field = {2: "image_item", 4: "file_item", 5: "video_item"}[kind]
    if kind == 4:
        descriptor["file_name"] = "../report.docx"
    if key_encoding == "image_override":
        descriptor["aeskey"] = key.hex()
        cdn["aes_key"] = "invalid-base64"
    if key_encoding == "plain":
        cdn.pop("aes_key")
    items = [{"type": kind, field: descriptor} for _ in range(count)]
    if caption:
        items.insert(0, {"type": 1, "text_item": {"text": caption}})
    return {
        "message_id": "poll-1",
        "from_user_id": "sender",
        "session_id": "session",
        "context_token": "reply-secret",
        "item_list": items,
    }


@pytest.mark.parametrize(
    ("kind", "encoding"), [(2, "raw"), (2, "image_override"), (2, "plain"), (4, "raw"), (4, "hex"), (5, "hex")]
)
@pytest.mark.parametrize("caption", ["", "review this"])
async def test_ilink_processor_stores_decrypted_refs_without_credentials(intake, kind, encoding, caption):
    config, wire, replay, accept, _ = intake
    plaintext = b"original-office-image-video"
    wire.payload = (
        plaintext if encoding == "plain" else AES.new(b"0123456789abcdef", AES.MODE_ECB).encrypt(pad(plaintext, 16))
    )
    await wechat_message_processor.process_wechat_message(
        config.agent_id, message(kind, encoding, caption=caption), config
    )
    native, accepted = replay.await_args.args[0], accept.await_args.args[0]
    assert accepted.fingerprint() == native.fingerprint()
    assert native.text == caption
    assert native.media_ids
    attachment = accepted.attachments[0]
    assert await facade.get_storage_backend().read_bytes(f"{config.agent_id}/{attachment.path}") == plaintext
    assert attachment.name == attachment.path.rsplit("/", 1)[1]
    assert "/" not in attachment.name
    content = accept.await_args.kwargs["saved_content"]
    assert content == f"{caption}\n[file:{attachment.path}]".strip()
    assert "secret" not in repr(accepted) + content
    assert wire.requests[0].url.host == "novac2c.cdn.weixin.qq.com"
    assert wire.requests[0].url.params["encrypted_query_param"] == "signed+query/secret"
    assert "authorization" not in wire.requests[0].headers


@pytest.mark.parametrize("gate", ["replay", "authorization", "missing_context", "missing_token"])
async def test_ilink_media_auth_and_replay_precede_downloads(intake, gate):
    config, wire, replay, accept, context = intake
    raw = message()
    if gate == "replay":
        replay.return_value = True
    if gate == "authorization":
        replay.side_effect = HTTPException(403, "revoked")
    if gate == "missing_context":
        raw.pop("context_token")
    if gate == "missing_token":
        config.extra_config = {}
    if gate == "authorization":
        with pytest.raises(HTTPException):
            await wechat_message_processor.process_wechat_message(config.agent_id, raw, config)
    else:
        await wechat_message_processor.process_wechat_message(config.agent_id, raw, config)
    assert wire.requests == []
    accept.assert_not_awaited()
    context.assert_not_awaited()


async def test_ilink_media_without_a_message_id_is_skipped_with_a_warning_before_any_work(intake, monkeypatch):
    config, wire, replay, accept, context = intake
    logs = Mock()
    monkeypatch.setattr(wechat_message_processor, "logger", logs)
    raw = message()
    raw["message_id"] = ""
    await wechat_message_processor.process_wechat_message(config.agent_id, raw, config)
    logs.warning.assert_called_once()
    assert wire.requests == []
    replay.assert_not_awaited()
    context.assert_not_awaited()
    accept.assert_not_awaited()


@pytest.mark.parametrize("stage", ["replay", "accept"])
async def test_ilink_media_from_an_unlinked_sender_is_skipped_with_an_info_log_and_nothing_raised(
    intake, monkeypatch, stage
):
    config, wire, replay, accept, _ = intake
    wire.payload = AES.new(b"0123456789abcdef", AES.MODE_ECB).encrypt(pad(b"file", 16))
    logs = Mock()
    monkeypatch.setattr(wechat_message_processor, "logger", logs)
    {"replay": replay, "accept": accept}[stage].side_effect = ChannelUserResolutionError("unlinked")
    await wechat_message_processor.process_wechat_message(config.agent_id, message(), config)
    logs.info.assert_called_once()
    logs.warning.assert_not_called()
    # An unlinked sender is refused by the replay check before the file is fetched.
    assert len(wire.requests) == (0 if stage == "replay" else 1)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/private",
        "https://attacker.example/a",
        "https://novac2c.cdn.weixin.qq.com.evil/a",
        "http://novac2c.cdn.weixin.qq.com/a",
    ],
)
async def test_ilink_full_url_is_pinned_to_provider_origin(intake, url):
    config, wire, _, accept, _ = intake
    raw = message(url=url)
    with pytest.raises(OutboundPolicyError):
        await wechat_message_processor.process_wechat_message(config.agent_id, raw, config)
    assert wire.requests == []
    accept.assert_not_awaited()


@pytest.mark.parametrize("failure", ["redirect", "provider", "crypto", "storage"])
async def test_ilink_provider_crypto_and_storage_failures_propagate(intake, monkeypatch, failure):
    config, wire, _, accept, _ = intake
    wire.payload = AES.new(b"0123456789abcdef", AES.MODE_ECB).encrypt(pad(b"payload", 16))
    expected = HTTPException
    if failure == "redirect":
        wire.status, wire.headers, expected = 302, {"Location": "https://attacker.example/steal"}, httpx.HTTPStatusError
    if failure == "provider":
        wire.status, expected = 500, httpx.HTTPStatusError
    if failure == "crypto":
        wire.payload, expected = b"broken-ciphertext", ValueError
    if failure == "storage":
        monkeypatch.setattr(
            facade.get_storage_backend(), "create_bytes_if_absent", AsyncMock(side_effect=HTTPException(507, "quota"))
        )
    with pytest.raises(expected):
        await wechat_message_processor.process_wechat_message(config.agent_id, message(), config)
    assert len(wire.requests) == 1
    accept.assert_not_awaited()


@pytest.mark.parametrize("overflow", [False, True])
async def test_ilink_image_exact_10mib_limit_after_decryption(intake, overflow):
    config, wire, _, accept, _ = intake
    plaintext = b"x" * (10 * 1024 * 1024 + int(overflow))
    wire.payload = AES.new(b"0123456789abcdef", AES.MODE_ECB).encrypt(pad(plaintext, 16))
    await wechat_message_processor.process_wechat_message(config.agent_id, message(2), config)
    event = accept.await_args.args[0]
    assert len(event.attachments) == (0 if overflow else 1)
    if overflow:
        assert "skipped" in accept.await_args.kwargs["saved_content"]


@pytest.mark.parametrize("cap", ["count", "event"])
async def test_ilink_count_and_50mib_event_limits_stop_more_downloads(intake, cap):
    config, wire, _, accept, _ = intake
    count = 11 if cap == "count" else 6
    wire.payload = b"x" if cap == "count" else b"x" * (10 * 1024 * 1024)
    await wechat_message_processor.process_wechat_message(config.agent_id, message(2, "plain", count=count), config)
    event = accept.await_args.args[0]
    assert len(wire.requests) == len(event.attachments) == (10 if cap == "count" else 5)
    assert len({item.path for item in event.attachments}) == len(event.attachments)
    assert "skipped" in accept.await_args.kwargs["saved_content"]


async def test_ilink_limit_stops_downloads_keeps_the_caption_and_the_original_fingerprint(intake, monkeypatch):
    # Given: a caption with three small images and a limit of two
    config, wire, replay, accept, _ = intake
    set_attachment_limit(monkeypatch, 2)
    wire.payload = b"x"
    # When
    await wechat_message_processor.process_wechat_message(
        config.agent_id, message(2, "plain", count=3, caption="question first"), config
    )
    # Then: only two downloads and stored files, the caption first and one notice last
    native = replay.await_args.args[0]
    event = accept.await_args.args[0]
    assert len(wire.requests) == len(event.attachments) == 2
    assert accept.await_args.kwargs["saved_content"] == "\n".join(
        ["question first", *(f"[file:{item.path}]" for item in event.attachments), skipped_attachments_note(1)]
    )
    assert accept.await_args.kwargs["content"] == accept.await_args.kwargs["saved_content"]
    # And: the receipt still sees all three images
    assert len(native.media_ids) == 3
    assert event.fingerprint() == native.fingerprint()


@pytest.mark.parametrize("storage_fails", [False, True])
async def test_poll_cursor_commits_only_after_media_storage(intake, monkeypatch, storage_fails):
    config, wire, _, accept, _ = intake
    wire.payload = AES.new(b"0123456789abcdef", AES.MODE_ECB).encrypt(pad(b"file", 16))
    manager = wechat_channel.WeChatPollManager()
    monkeypatch.setattr(manager, "_load_config", AsyncMock(return_value=config))
    monkeypatch.setattr(manager, "_set_connected", AsyncMock())
    update = AsyncMock()
    monkeypatch.setattr(manager, "_update_extra", update)
    monkeypatch.setattr(
        manager,
        "_fetch_updates",
        AsyncMock(side_effect=[{"get_updates_buf": "next", "msgs": [message()]}, asyncio.CancelledError()]),
    )
    if storage_fails:
        monkeypatch.setattr(
            facade.get_storage_backend(), "create_bytes_if_absent", AsyncMock(side_effect=HTTPException(507, "quota"))
        )
        monkeypatch.setattr(wechat_channel.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await manager._run_client(config.agent_id)
    if storage_fails:
        update.assert_not_awaited()
        accept.assert_not_awaited()
    else:
        update.assert_awaited_once_with(config.agent_id, {"get_updates_buf": "next"})
        assert accept.await_args.args[0].attachments
