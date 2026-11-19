import base64
import re
from unittest.mock import AsyncMock

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException

from app.api import wecom
from app.core.json_types import json_object_from
from app.services import wecom_stream
from app.services.channels import wecom_media
from app.services.channels.media_limits import skipped_attachments_note
from app.services.channels.stream_source import stream_source
from app.services.storage_runtime import facade
from channel_media_fixtures import set_attachment_limit
from im_webhook_fixtures import AGENT_ID
from stream_source_fixtures import connection_config

pytest_plugins = ("test_wecom_media_intake",)


@pytest.mark.parametrize("cap", ["count", "event"])
async def test_stream_mixed_count_and_event_caps_stop_downloads(intake, monkeypatch, cap):
    _, wire, _, accept, _ = intake
    config = connection_config(monkeypatch, AGENT_ID, "wecom")
    key = b"k" * 32
    plaintext = b"x" if cap == "count" else b"x" * (10 * 1024 * 1024)
    wire.payload = AES.new(key, AES.MODE_CBC, key[:16]).encrypt(pad(plaintext, 32))
    descriptor = {"url": wecom_media.WECOM_STREAM_ORIGIN + "/signed", "aeskey": base64.b64encode(key).decode()}
    body = {
        "msgtype": "mixed",
        "mixed": {"msg_item": [{"type": "image", "image": descriptor} for _ in range(11 if cap == "count" else 6)]},
    }
    await wecom_stream._process_wecom_stream_message(
        AGENT_ID,
        "sender",
        "",
        message_id="mixed-1",
        source=stream_source(config),
        media_body=json_object_from(body),
    )
    event = accept.await_args.args[0]
    assert len(wire.requests) == len(event.attachments) == (10 if cap == "count" else 5)
    assert "skipped" in accept.await_args.kwargs["saved_content"]


@pytest.mark.parametrize("failure", ["redirect", "json_error", "bad_key", "bad_padding"])
async def test_wecom_download_failures_propagate_without_accepted_refs(intake, failure):
    config, wire, _, accept, _ = intake
    expected = ValueError
    if failure == "redirect":
        wire.status, wire.headers, expected = 302, {"Location": "https://attacker.example"}, httpx.HTTPStatusError
    elif failure == "json_error":
        wire.payload, wire.headers, expected = b'{"errcode":40007}', {"Content-Type": "application/json"}, HTTPException
    media = wecom_media.WeComMedia("file", "report.docx", media_id="resource-id")
    if failure in {"bad_key", "bad_padding"}:
        key = b"k" * (16 if failure == "bad_key" else 32)
        media = wecom_media.WeComMedia(
            "file",
            "report.docx",
            url=wecom_media.WECOM_STREAM_ORIGIN + "/signed",
            aes_key=base64.b64encode(key).decode(),
        )
        wire.payload = b"invalid-padding!"
    with pytest.raises(expected):
        await wecom._process_wecom_text(
            config.agent_id, config, "sender", "", message_id="native", kind="file", media=(media,)
        )
    accept.assert_not_awaited()


async def test_wecom_resource_above_20mib_uses_aligned_ranges_and_preserves_bytes(intake, monkeypatch):
    config, _, _, accept, _ = intake
    payload = b"a" * (20 * 1024 * 1024) + b"tail"
    requests = []

    def respond(request):
        requests.append(request)
        bounds = re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["Range"])
        assert bounds is not None
        start, end = int(bounds[1]), min(int(bounds[2]), len(payload) - 1)
        return httpx.Response(
            206,
            content=payload[start : end + 1],
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(payload)}",
                "Content-Disposition": 'attachment; filename="large.xlsx"',
            },
        )

    monkeypatch.setattr(
        wecom_media, "public_http_client", lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    await wecom._process_wecom_text(
        config.agent_id,
        config,
        "sender",
        "",
        message_id="native",
        kind="file",
        media=(wecom_media.WeComMedia("file", "file.bin", media_id="large"),),
    )
    event = accept.await_args.args[0]
    assert [request.headers["Range"] for request in requests] == ["bytes=0-20971519", "bytes=20971520-41943039"]
    assert await facade.get_storage_backend().read_bytes(f"{config.agent_id}/{event.attachments[0].path}") == payload


async def test_wecom_range_total_above_50mib_is_skipped_before_read(intake):
    config, wire, _, accept, _ = intake
    wire.status = 206
    wire.headers = {"Content-Range": "bytes 0-20971519/52428801"}
    await wecom._process_wecom_text(
        config.agent_id,
        config,
        "sender",
        "",
        message_id="native",
        kind="file",
        media=(wecom_media.WeComMedia("file", "file.bin", media_id="large"),),
    )
    assert len(wire.requests) == 1
    assert accept.await_args.args[0].attachments == ()
    assert "skipped" in accept.await_args.kwargs["saved_content"]


async def test_stream_mixed_limit_keeps_every_text_and_skips_requests_and_decryption(intake, monkeypatch):
    # Given: a mixed message with text around three images and a limit of two
    _, wire, replay, accept, _ = intake
    set_attachment_limit(monkeypatch, 2)
    config = connection_config(monkeypatch, AGENT_ID, "wecom")
    key = b"k" * 32
    wire.payload = AES.new(key, AES.MODE_CBC, key[:16]).encrypt(pad(b"x", 32))
    decrypted = []
    decrypt = wecom_media.decrypt_file

    def counting_decrypt(data, aes_key):
        decrypted.append(aes_key)
        return decrypt(data, aes_key)

    monkeypatch.setattr(wecom_media, "decrypt_file", counting_decrypt)

    def image(index):
        url = f"{wecom_media.WECOM_STREAM_ORIGIN}/signed-{index}"
        return {"msgtype": "image", "image": {"url": url, "aeskey": base64.b64encode(key).decode()}}

    def text(value):
        return {"msgtype": "text", "text": {"content": value}}

    body = {
        "msgtype": "mixed",
        "mixed": {
            "msg_item": [text("before"), image(1), text("middle"), image(2), image(3), text("question at the end")]
        },
    }
    # When
    await wecom_stream._process_wecom_stream_message(
        AGENT_ID,
        "sender",
        "",
        message_id="mixed-2",
        source=stream_source(config),
        media_body=json_object_from(body),
    )
    # Then: the third image was never requested or decrypted, and all text is kept ahead of the one notice
    event = accept.await_args.args[0]
    assert [request.url.path for request in wire.requests] == ["/signed-1", "/signed-2"]
    assert len(decrypted) == len(event.attachments) == 2
    assert event.text == "before\nmiddle\nquestion at the end"
    assert accept.await_args.kwargs["saved_content"] == "\n".join(
        [event.text, *(f"[file:{item.path}]" for item in event.attachments), skipped_attachments_note(1)]
    )
    # And: the receipt still sees all three images
    assert len(replay.await_args.args[0].media_ids) == 3
    assert event.fingerprint() == replay.await_args.args[0].fingerprint()


async def test_wecom_media_ids_beyond_the_limit_trigger_no_token_lookup_or_download(intake, monkeypatch):
    # Given: three downloadable resources and a limit of two
    config, wire, replay, accept, _ = intake
    set_attachment_limit(monkeypatch, 2)
    media = tuple(wecom_media.WeComMedia("file", f"file{index}.bin", media_id=f"media-{index}") for index in range(3))
    # When
    await wecom._process_wecom_text(
        config.agent_id, config, "sender", "caption", message_id="native", kind="file", media=media
    )
    # Then: only the first two resources cost a credential lookup and a download
    token = wecom_media.get_wecom_access_token
    assert isinstance(token, AsyncMock)
    assert token.await_count == 2
    assert [request.url.params["media_id"] for request in wire.requests] == ["media-0", "media-1"]
    assert replay.await_args.args[0].media_ids == ("media-0", "media-1", "media-2")
    assert accept.await_args.kwargs["saved_content"].count(skipped_attachments_note(1)) == 1
