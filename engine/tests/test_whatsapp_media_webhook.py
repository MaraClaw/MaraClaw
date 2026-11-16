import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import whatsapp
from app.services.channels import submissions, whatsapp_cloud_media
from app.services.channels.submissions import AcceptedChannelEvent
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.local import LocalStorageBackend


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
