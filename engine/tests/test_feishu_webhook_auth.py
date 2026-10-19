import base64
import hashlib
import json
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import feishu


def _encrypted_body(payload: object, key: str) -> bytes:
    iv = b"0123456789abcdef"
    cipher = AES.new(hashlib.sha256(key.encode()).digest(), AES.MODE_CBC, iv)
    encrypted = iv + cipher.encrypt(pad(json.dumps(payload).encode(), AES.block_size))
    return json.dumps({"encrypt": base64.b64encode(encrypted).decode()}).encode()


def _headers(body: bytes, key: str) -> dict[str, str]:
    return {
        "x-lark-request-timestamp": "123",
        "x-lark-request-nonce": "nonce",
        "x-lark-signature": hashlib.sha256(("123nonce" + key).encode() + body).hexdigest(),
    }


@pytest.mark.parametrize("schema", ["1.0", "2.0"])
@pytest.mark.parametrize("encrypted", [False, True])
async def test_feishu_dispatches_authenticated_event(monkeypatch, schema, encrypted):
    config = replace(channel_config("feishu"), encrypt_key="encrypt-key" if encrypted else "")
    payload = {"schema": schema, "event": {"text": "customer message"}}
    payload["header" if schema == "2.0" else "token"] = (
        {"token": config.verification_token} if schema == "2.0" else config.verification_token
    )
    body = _encrypted_body(payload, config.encrypt_key or "") if encrypted else json.dumps(payload).encode()
    dispatch = AsyncMock(return_value={"code": 0})
    monkeypatch.setattr(feishu.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu, "process_feishu_event", dispatch)
    async with webhook_client(feishu.router) as client:
        response = await client.post(
            f"/api/channel/feishu/{AGENT_ID}/webhook", content=body, headers=_headers(body, config.encrypt_key or "")
        )
    assert response.status_code == 200
    dispatch.assert_awaited_once_with(AGENT_ID, payload)


@pytest.mark.parametrize("encrypted", [False, True])
async def test_feishu_echoes_only_authenticated_challenge(monkeypatch, encrypted):
    config = replace(channel_config("feishu"), encrypt_key="encrypt-key" if encrypted else "")
    payload = {"type": "url_verification", "token": config.verification_token, "challenge": "provider-challenge"}
    body = _encrypted_body(payload, config.encrypt_key or "") if encrypted else json.dumps(payload).encode()
    monkeypatch.setattr(feishu.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    dispatch = AsyncMock()
    monkeypatch.setattr(feishu, "process_feishu_event", dispatch)
    async with webhook_client(feishu.router) as client:
        response = await client.post(f"/api/channel/feishu/{AGENT_ID}/webhook", content=body)
    assert response.json() == {"challenge": "provider-challenge"}
    dispatch.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        "missing_token",
        "forged_token",
        "legacy",
        "missing_signature",
        "forged_signature",
        "malformed_header",
        "invalid_json",
        "plaintext_with_key",
        "invalid_ciphertext",
        "wrong_encrypt_key",
        "forged_challenge",
        "missing_challenge",
        "non_ascii_signature",
        "non_object",
    ],
)
async def test_feishu_rejects_before_any_event_side_effect(monkeypatch, failure):
    config = replace(channel_config("feishu"), encrypt_key="")
    payload = {"schema": "2.0", "header": {"token": config.verification_token}, "event": {"text": "private-message"}}
    if failure == "missing_token":
        payload["header"] = {}
    if failure == "forged_token":
        payload["header"] = {"token": "forged"}
    if failure == "forged_challenge":
        payload = {"type": "url_verification", "token": "forged", "challenge": "private-message"}
    if failure == "missing_challenge":
        payload = {"type": "url_verification", "token": config.verification_token, "event": {"text": "private-message"}}
    if failure == "legacy":
        config.verification_token = None
    body = json.dumps(payload).encode()
    if failure in {"plaintext_with_key", "invalid_ciphertext", "wrong_encrypt_key"}:
        config.encrypt_key = "encrypt-key"
    if failure == "invalid_ciphertext":
        body = b'{"encrypt":"not-base64"}'
    if failure == "wrong_encrypt_key":
        body = _encrypted_body(payload, "different-key")
    if failure == "invalid_json":
        body = b"\xffinvalid-json"
    if failure == "non_object":
        body = b"[]"
    headers = _headers(body, config.encrypt_key or "")
    if failure == "missing_signature":
        headers = {}
    if failure == "forged_signature":
        headers["x-lark-signature"] = "0" * 64
    if failure == "malformed_header":
        headers["x-lark-signature"] = "not-hex"
    if failure == "non_ascii_signature":
        headers = {b"x-lark-signature": b"\xff", b"x-lark-request-timestamp": b"123", b"x-lark-request-nonce": b"nonce"}
    dispatch, dedup, update, logs = AsyncMock(), AsyncMock(), AsyncMock(), Mock()
    monkeypatch.setattr(feishu.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu.channel_config_dao, "update", update)
    monkeypatch.setattr(feishu.channel_dedup, "already_processed_shared", dedup)
    monkeypatch.setattr(feishu, "process_feishu_event", dispatch)
    monkeypatch.setattr(feishu, "logger", logs)
    async with webhook_client(feishu.router) as client:
        response = await client.post(f"/api/channel/feishu/{AGENT_ID}/webhook", content=body, headers=headers)
    assert response.status_code == 401
    assert not dispatch.called
    assert not dedup.called
    assert not update.called
    assert not logs.mock_calls


@pytest.mark.parametrize("configured", [False, True])
async def test_feishu_webhook_is_absent_for_websocket_or_missing_row(monkeypatch, configured):
    config = replace(channel_config("feishu"), extra_config={"connection_mode": "websocket"}) if configured else None
    monkeypatch.setattr(feishu.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    async with webhook_client(feishu.router) as client:
        response = await client.post(f"/api/channel/feishu/{AGENT_ID}/webhook", json={"challenge": "probe"})
    assert response.status_code == 404
