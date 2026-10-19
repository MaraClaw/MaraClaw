import hashlib
import hmac
from unittest.mock import AsyncMock, Mock

import pytest
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import whatsapp
from app.services.channels import dedup

BODY = b'{"entry":[{"changes":[{"value":{"messages":[{"id":"wa-event","type":"reaction"}]}}]}]}'


@pytest.mark.parametrize(
    "failure", [None, "missing", "forged", "malformed", "non_ascii", "legacy", "invalid_json", "modified_body"]
)
async def test_whatsapp_authenticates_raw_body_before_dedup(monkeypatch, failure):
    config = channel_config("whatsapp")
    body = b"invalid-json" if failure == "invalid_json" else BODY
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    headers = {"x-hub-signature-256": signature}
    if failure == "missing":
        headers = {}
    if failure == "forged":
        headers["x-hub-signature-256"] = "sha256=" + "0" * 64
    if failure == "malformed":
        headers["x-hub-signature-256"] = "sha256=not-hex"
    if failure == "non_ascii":
        headers = {b"x-hub-signature-256": b"sha256=\xff"}
    if failure == "legacy":
        config.encrypt_key = None
    if failure == "modified_body":
        body += b" "
    seen, mark, update, dispatch, logs = AsyncMock(return_value=False), AsyncMock(), AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(whatsapp.channel_config_dao, "update", update)
    monkeypatch.setattr(whatsapp.agent_dao, "get", dispatch)
    monkeypatch.setattr(dedup, "already_processed_shared", seen)
    monkeypatch.setattr(dedup, "mark_processed_shared", mark)
    monkeypatch.setattr(whatsapp, "logger", logs)
    async with webhook_client(whatsapp.router) as client:
        response = await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=body, headers=headers)
    if failure is None:
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mark.assert_awaited_once_with("whatsapp", "wa-event")
    else:
        assert response.status_code == 401
        assert not seen.called
        assert not mark.called
        assert not update.called
        assert not dispatch.called
        assert not logs.mock_calls


async def test_whatsapp_unconfigured_callback_is_not_found(monkeypatch):
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    async with webhook_client(whatsapp.router) as client:
        response = await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=BODY)
    assert response.status_code == 404
