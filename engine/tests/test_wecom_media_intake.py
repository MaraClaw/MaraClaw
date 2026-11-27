import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import wecom_aibot_sdk
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi import HTTPException
from wecom_aibot_sdk import WSClient

from app.api import wecom
from app.core.outbound_policy import OutboundPolicyError
from app.services import wecom_stream
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import submissions, wecom_media
from app.services.storage_runtime import facade
from im_webhook_fixtures import AGENT_ID, channel_config, webhook_client
from stream_source_fixtures import connection_config


@pytest.fixture
def intake(monkeypatch, tmp_path):
    config = channel_config("wecom")
    config.encrypt_key = base64.b64encode(b"a" * 32).decode().rstrip("=")
    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(facade, "_storage_backend", None)
    monkeypatch.setattr(
        facade,
        "get_settings",
        lambda: SimpleNamespace(STORAGE_BACKEND="local", STORAGE_LOCAL_ROOT=str(tmp_path), AGENT_DATA_DIR="unused"),
    )
    replay, accept = AsyncMock(return_value=False), AsyncMock()
    monkeypatch.setattr(wecom_media, "is_channel_event_replay", replay)
    monkeypatch.setattr(wecom_media, "accept_channel_event", accept)
    monkeypatch.setattr(
        wecom_media, "get_wecom_access_token", AsyncMock(return_value={"access_token": "provider-token"})
    )
    wire = SimpleNamespace(requests=[], payload=b"office-document", status=200, headers={}, sync=[])

    def respond(request):
        wire.requests.append(request)
        if request.url.path.endswith("sync_msg"):
            return httpx.Response(200, json={"errcode": 0, "msg_list": wire.sync, "has_more": 0})
        return httpx.Response(wire.status, content=wire.payload, headers=wire.headers)

    original = httpx.AsyncClient
    monkeypatch.setattr(wecom_media, "public_http_client", lambda **_: original(transport=httpx.MockTransport(respond)))
    return config, wire, replay, accept, respond


async def post_xml(client, config, inner, signature=None):
    encrypted = wecom._encrypt_msg(config.encrypt_key, inner, config.app_id)
    signature = signature or wecom._verify_signature(config.verification_token, "1", "2", encrypted)
    return await client.post(
        f"/api/channel/wecom/{config.agent_id}/webhook",
        params={"msg_signature": signature, "timestamp": "1", "nonce": "2"},
        content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
    )


@pytest.mark.parametrize(("kind", "name"), [("image", "photo.jpg"), ("file", "report.docx"), ("video", "clip.mp4")])
@pytest.mark.parametrize("caption", ["", "review this"])
async def test_signed_xml_media_stores_canonical_refs_and_preserves_native_fingerprint(intake, kind, name, caption):
    config, wire, replay, accept, _ = intake
    wire.headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    inner = (
        f"<xml><MsgType>{kind}</MsgType><MsgId>native-1</MsgId><FromUserName>sender</FromUserName>"
        f"<MediaId>media-1</MediaId><Content>{caption}</Content></xml>"
    )
    async with webhook_client(wecom.router) as client:
        response = await post_xml(client, config, inner)
    assert response.status_code == 200
    native = replay.await_args.args[0]
    event = accept.await_args.args[0]
    assert native.kind == kind
    assert native.text == caption
    assert native.media_ids == ("media-1",)
    assert event.fingerprint() == native.fingerprint()
    assert event.attachments[0].name == name
    assert (
        await facade.get_storage_backend().read_bytes(f"{config.agent_id}/{event.attachments[0].path}") == wire.payload
    )
    assert accept.await_args.kwargs["saved_content"] == f"{caption}\n[file:{event.attachments[0].path}]".strip()
    assert wire.requests[0].url.host == "qyapi.weixin.qq.com"
    assert wire.requests[0].url.params["access_token"] == "provider-token"
    assert wire.requests[0].headers["Range"] == "bytes=0-20971519"


@pytest.mark.parametrize("gate", ["signature", "replay", "authorization", "provider", "oversize"])
async def test_xml_failure_and_replay_boundaries_have_no_accepted_files(intake, gate):
    config, wire, replay, accept, _ = intake
    if gate == "replay":
        replay.return_value = True
    if gate == "authorization":
        replay.side_effect = HTTPException(403, "revoked")
    if gate == "provider":
        wire.status = 500
    if gate == "oversize":
        wire.headers = {"Content-Length": str(10 * 1024 * 1024 + 1)}
    inner = "<xml><MsgType>image</MsgType><MsgId>m</MsgId><FromUserName>s</FromUserName><MediaId>id</MediaId></xml>"
    async with webhook_client(wecom.router) as client:
        response = await post_xml(client, config, inner, "bad" if gate == "signature" else None)
    assert response.status_code == ({"signature": 403, "authorization": 503, "provider": 503}.get(gate, 200))
    if gate in {"signature", "replay", "authorization"}:
        assert wire.requests == []
    if gate == "oversize":
        assert accept.await_args.args[0].attachments == ()
        assert "skipped" in accept.await_args.kwargs["saved_content"]
    else:
        accept.assert_not_awaited()


@pytest.mark.parametrize("kind", ["image", "file", "video"])
async def test_kf_sync_media_keeps_kf_account_identity(intake, monkeypatch, kind):
    config, wire, replay, accept, respond = intake
    wire.sync = [
        {
            "origin": 3,
            "msgtype": kind,
            "msgid": "kf-1",
            "external_userid": "external",
            "open_kfid": "kf-account",
            kind: {"media_id": "kf-media", "file_name": "sheet.xlsx"},
        }
    ]
    original = httpx.AsyncClient
    async with webhook_client(wecom.router) as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
        monkeypatch.setattr(wecom, "get_wecom_access_token", AsyncMock(return_value={"access_token": "provider-token"}))
        response = await post_xml(
            client, config, "<xml><MsgType>event</MsgType><Event>kf_msg_or_event</Event><Token>cursor</Token></xml>"
        )
    assert response.status_code == 200
    event = accept.await_args.args[0]
    assert event.kind == f"kf_{kind}"
    assert event.sender_id == "external"
    assert "kf-account" in event.event_key
    assert "kf-1" in event.event_key
    assert event.fingerprint() == replay.await_args.args[0].fingerprint()
    assert event.attachments


@pytest.mark.parametrize("kind", ["image", "file", "mixed", "video"])
async def test_installed_sdk_media_handler_decrypts_and_skips_replay(intake, monkeypatch, kind):
    _, wire, replay, accept, _ = intake
    config = connection_config(monkeypatch, AGENT_ID, "wecom")
    key = b"k" * 32
    wire.payload = AES.new(key, AES.MODE_CBC, key[:16]).encrypt(pad(b"decrypted", 32))
    clients, connected = [], asyncio.Event()

    def construct(options):
        client = WSClient(options)
        monkeypatch.setattr(client, "connect_async", AsyncMock(side_effect=connected.set))
        clients.append(client)
        return client

    monkeypatch.setattr(wecom_aibot_sdk, "WSClient", construct)
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    monkeypatch.setattr(wecom_stream, "_reply_stream", AsyncMock())
    body = {
        "msgid": "stream-1",
        "msgtype": kind,
        "from_userid": "sender",
        "file": {
            "url": wecom_media.WECOM_STREAM_ORIGIN + "/signed?secret=opaque",
            "aeskey": base64.b64encode(key).decode(),
            "name": "report.pptx",
        },
    }
    body["image"] = body["file"]
    body["video"] = body["file"]
    if kind == "mixed":
        body["mixed"] = {
            "msg_item": [{"type": "text", "text": {"content": "caption"}}, {"type": "image", "image": body["image"]}]
        }
    manager = wecom_stream.WeComStreamManager()
    try:
        await manager.start_client(AGENT_ID, "bot", "secret")
        await asyncio.wait_for(connected.wait(), 2)
        client = clients[0]
        frame = client._message_handler.parse_frame({"headers": {"req_id": "r"}, "body": body})
        handler = client._message_handler._event_handlers["message" if kind == "video" else f"message.{kind}"][0]
        await handler(frame)
        event = accept.await_args.args[0]
        assert (
            await facade.get_storage_backend().read_bytes(f"{config.agent_id}/{event.attachments[0].path}")
            == b"decrypted"
        )
        assert event.fingerprint() == replay.await_args.args[0].fingerprint()
        assert "secret=opaque" not in repr(event) + repr(accept.await_args.kwargs)
        assert "authorization" not in wire.requests[0].headers
        replay.return_value = True
        await handler(frame)
        assert len(wire.requests) == 1
        assert accept.await_count == 1
        config.extra_config = {**config.extra_config, "bot_secret": "revoked"}
        with pytest.raises(HTTPException):
            await handler(frame)
    finally:
        await manager.stop_all()
    assert manager._tasks == {}


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/private",
        "https://attacker.example/file",
        "https://ww-aibot-img-1258476243.cos.ap-guangzhou.myqcloud.com.evil/file",
    ],
)
async def test_stream_media_untrusted_origin_is_rejected_before_storage(intake, monkeypatch, url):
    config, _, _, accept, _ = intake
    from app.core.outbound_transport import public_http_client

    monkeypatch.setattr(wecom_media, "public_http_client", public_http_client)
    native = submissions.ChannelEvent(config.id, config.agent_id, "wecom", "m", "s", "c", "", kind="file")
    media = wecom_media.WeComMedia("file", "file.docx", url=url, aes_key=base64.b64encode(b"k" * 32).decode())
    with pytest.raises(OutboundPolicyError):
        await wecom_media.accept_wecom_media(native, config, (media,))
    accept.assert_not_awaited()


@pytest.fixture
def text_accept(monkeypatch):
    accept = AsyncMock()
    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    return accept


def message_xml(kind, msg_id=""):
    ids = f"<MsgId>{msg_id}</MsgId>" if msg_id else ""
    body = "<Content>hello</Content>" if kind == "text" else "<MediaId>media-1</MediaId>"
    return f"<xml><MsgType>{kind}</MsgType>{ids}<FromUserName>sender</FromUserName>{body}</xml>"


@pytest.mark.parametrize("kind", ["text", "image", "file", "video"])
async def test_callback_message_without_msgid_is_refused_before_acceptance_or_download(intake, text_accept, kind):
    config, wire, replay, accept, _ = intake
    async with webhook_client(wecom.router) as client:
        response = await post_xml(client, config, message_xml(kind))
    assert response.status_code == 400
    assert wire.requests == []
    for refused in (replay, accept, text_accept):
        refused.assert_not_awaited()


@pytest.mark.parametrize("kind", ["text", "image"])
async def test_callback_from_an_unlinked_sender_is_acknowledged_without_acceptance(intake, text_accept, kind):
    config, wire, replay, accept, _ = intake
    unlinked = ChannelUserResolutionError("Link your channel identity in the web application.")
    text_accept.side_effect = replay.side_effect = unlinked
    async with webhook_client(wecom.router) as client:
        response = await post_xml(client, config, message_xml(kind, "native-1"))
    assert (response.status_code, response.content) == (200, b"success")
    (text_accept if kind == "text" else replay).assert_awaited_once()
    assert wire.requests == []
    accept.assert_not_awaited()


def kf_text(msg_id, sender="customer"):
    message = {
        "origin": 3,
        "msgtype": "text",
        "external_userid": sender,
        "open_kfid": "kf-account",
        "text": {"content": "hello"},
    }
    return {**message, "msgid": msg_id} if msg_id else message


def kf_image(msg_id, media_id):
    item = {"media_id": media_id} if media_id else {}
    return {
        "origin": 3,
        "msgtype": "image",
        "msgid": msg_id,
        "external_userid": "customer",
        "open_kfid": "kf-account",
        "image": item,
    }


async def post_kf_page(intake, monkeypatch, page):
    config, wire, *_, respond = intake
    wire.sync = page
    original = httpx.AsyncClient
    async with webhook_client(wecom.router) as client:
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
        monkeypatch.setattr(wecom, "get_wecom_access_token", AsyncMock(return_value={"access_token": "provider-token"}))
        return await post_xml(
            client, config, "<xml><MsgType>event</MsgType><Event>kf_msg_or_event</Event><Token>cursor</Token></xml>"
        )


def accepted_ids(accept):
    return [json.loads(call.args[0].event_key)[2] for call in accept.await_args_list]


async def test_kf_page_skips_messages_it_cannot_accept_and_keeps_going(intake, text_accept, monkeypatch):
    _, wire, _, accept, _ = intake

    async def accept_text(event, **_):
        if event.sender_id == "stranger":
            raise ChannelUserResolutionError("Link your channel identity in the web application.")

    text_accept.side_effect = accept_text
    page = [
        kf_text("kf-good-1"),
        kf_text(""),
        kf_image("kf-no-media-id", ""),
        kf_text("kf-unlinked", sender="stranger"),
        kf_image("kf-good-2", "kf-media"),
    ]
    response = await post_kf_page(intake, monkeypatch, page)
    assert (response.status_code, response.content) == (200, b"success")
    assert accepted_ids(text_accept) == ["kf-good-1", "kf-unlinked"]
    assert accepted_ids(accept) == ["kf-good-2"]
    assert len(wire.requests) == 2


@pytest.mark.parametrize("error", [RuntimeError("database unavailable"), HTTPException(403, "revoked")])
async def test_kf_page_acceptance_failure_still_fails_the_callback_and_stops_the_page(
    intake, text_accept, monkeypatch, error
):
    async def accept_text(event, **_):
        if event.sender_id == "flaky":
            raise error

    text_accept.side_effect = accept_text
    page = [kf_text("kf-1"), kf_text("kf-2", sender="flaky"), kf_text("kf-3")]
    response = await post_kf_page(intake, monkeypatch, page)
    assert response.status_code == 503
    assert accepted_ids(text_accept) == ["kf-1", "kf-2"]
