from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from uuid import uuid4

import httpx
import pytest

from app.core.domain.channels import ChannelType
from app.core.outbound_policy import OutboundPolicyError
from app.records.channel_config import ChannelConfigRecord
from app.schemas.telegram import TelegramMessage
from app.services.channels import telegram_media, whatsapp_cloud_media
from app.services.channels.submissions import ChannelEvent
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.local import LocalStorageBackend


@pytest.mark.parametrize("provider", ["telegram", "whatsapp"])
@pytest.mark.parametrize(
    "filename",
    ["report.pdf", "report.doc", "report.docx", "deck.pptx", "sheet.xls", "sheet.xlsx", "clip.mp4", "photo.png"],
)
async def test_provider_media_download_stores_reference_without_changing_native_identity(
    monkeypatch, tmp_path, provider, filename
):
    config = ChannelConfigRecord(
        id=uuid4(), agent_id=uuid4(), channel_type=ChannelType(provider), app_id="123", app_secret="123:token"
    )
    event = ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider=provider,
        event_key="native",
        sender_id="42",
        conversation_id="direct",
        text="caption",
        kind="document",
        media_ids=("file1",),
    )
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file.bin"}})
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media/file1"})
        return httpx.Response(200, content=b"provider payload")

    @asynccontextmanager
    async def client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            yield http

    module = telegram_media if provider == "telegram" else whatsapp_cloud_media
    monkeypatch.setattr(module, "public_http_client", client)
    if provider == "telegram":
        message = TelegramMessage.model_validate(
            {
                "message_id": 1,
                "chat": {"id": 42, "type": "private"},
                "document": {"file_id": "file1", "file_name": filename},
            }
        )
        enriched, content = await telegram_media.enrich_media(config, event, message)
    else:
        enriched, content = await whatsapp_cloud_media.enrich_media(
            config, event, {"id": "file1", "filename": filename}
        )

    assert enriched.fingerprint() == event.fingerprint()
    assert len(enriched.attachments) == 1
    reference = enriched.attachments[0]
    assert reference.name == filename
    assert PurePosixPath(reference.path).name == filename
    assert await storage.read_bytes(f"{config.agent_id}/{reference.path}") == b"provider payload"
    assert content == f"caption\n[file:{reference.path}]"
    assert len(calls) == 2
    assert "provider payload" not in content
    assert "https:" not in content


async def test_whatsapp_metadata_cannot_redirect_credential_to_foreign_origin(monkeypatch):
    config = ChannelConfigRecord(
        id=uuid4(), agent_id=uuid4(), channel_type=ChannelType.WHATSAPP, app_id="123", app_secret="secret"
    )
    event = ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="whatsapp",
        event_key="native",
        sender_id="42",
        conversation_id="direct",
        text="",
        kind="image",
        media_ids=("file1",),
    )
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"url": "https://attacker.example/private"})

    @asynccontextmanager
    async def client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            yield http

    monkeypatch.setattr(whatsapp_cloud_media, "public_http_client", client)
    with pytest.raises(OutboundPolicyError, match="media destination"):
        await whatsapp_cloud_media.enrich_media(config, event, {"id": "file1"})
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["telegram", "whatsapp"])
async def test_provider_oversize_body_never_becomes_attachment(monkeypatch, tmp_path, provider):
    config = ChannelConfigRecord(
        id=uuid4(), agent_id=uuid4(), channel_type=ChannelType(provider), app_id="123", app_secret="123:token"
    )
    event = ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider=provider,
        event_key="large",
        sender_id="42",
        conversation_id="direct",
        text="caption",
        kind="image",
        media_ids=("file1",),
    )
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)

    def respond(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/file1"}})
        if request.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/media/file1"})
        return httpx.Response(200, headers={"content-length": str(10 * 1024 * 1024 + 1)}, content=b"")

    @asynccontextmanager
    async def client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            yield http

    module = telegram_media if provider == "telegram" else whatsapp_cloud_media
    monkeypatch.setattr(module, "public_http_client", client)
    if provider == "telegram":
        message = TelegramMessage.model_validate(
            {"message_id": 1, "chat": {"id": 42, "type": "private"}, "photo": [{"file_id": "file1"}]}
        )
        enriched, content = await telegram_media.enrich_media(config, event, message)
    else:
        enriched, content = await whatsapp_cloud_media.enrich_media(config, event, {"id": "file1"})
    assert enriched.attachments == ()
    assert enriched.fingerprint() == event.fingerprint()
    assert content.startswith("caption")
    assert not list(tmp_path.iterdir())
