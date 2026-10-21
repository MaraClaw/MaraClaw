import hashlib
import hmac
import json
from unittest.mock import AsyncMock, Mock

import pytest
from tests.channel_submission_fixtures import channel_db as channel_db, receipt_db as receipt_db
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import whatsapp
from app.db.session import transaction
from app.services.channels import dedup, submissions

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
    monkeypatch.setattr(submissions, "accept_channel_event", dispatch)
    monkeypatch.setattr(dedup, "already_processed_shared", seen)
    monkeypatch.setattr(dedup, "mark_processed_shared", mark)
    monkeypatch.setattr(whatsapp, "logger", logs)
    async with webhook_client(whatsapp.router) as client:
        response = await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=body, headers=headers)
    if failure is None:
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mark.assert_not_awaited()
        dispatch.assert_not_awaited()
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


async def test_signed_batch_continues_after_failure_and_redelivery_replays_committed_events(channel_db, monkeypatch):
    # Given nested signed events and a transient model-preparation failure in the middle.
    fixture = channel_db
    config = fixture.config
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    send = AsyncMock(side_effect=AssertionError("acceptance must not send an apology"))
    monkeypatch.setattr(whatsapp, "_send_whatsapp_messages", send)
    monkeypatch.setattr(dedup, "already_processed_shared", AsyncMock(return_value=True))
    original_accept, original_prepare = submissions.accept_channel_event, submissions.prepare_openclaw_message
    prepare_calls: list[str] = []
    fail_once = True

    async def prepare(*, agent, content, history):
        nonlocal fail_once
        prepare_calls.append(content)
        if content == "transient" and fail_once:
            fail_once = False
            raise RuntimeError("transient preparation failure")
        return await original_prepare(agent=agent, content=content, history=history)

    async def accept(event):
        async with transaction(fixture.db.first):
            return await original_accept(event)

    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    monkeypatch.setattr(submissions, "prepare_openclaw_message", prepare)
    async with transaction(fixture.db.first):
        await fixture.db.first.execute("UPDATE users SET quota_message_limit = 10")

    def change(*messages):
        return {
            "value": {
                "metadata": {"phone_number_id": "phone-account"},
                "messages": [
                    {"id": key, "from": "15550001", "type": "text", "text": {"body": text}} for key, text in messages
                ],
            }
        }

    body = json.dumps(
        {
            "entry": [
                {"changes": [change(("first", "hello"), ("middle", "transient")), change(("third", "hello"))]},
                {"changes": [change(("last", "hello"))]},
            ]
        }
    ).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    headers = {"x-hub-signature-256": signature}
    # When the provider retries the entire batch after the first retryable HTTP response.
    async with webhook_client(whatsapp.router) as client:
        first = await client.post(f"/api/channel/whatsapp/{config.agent_id}/webhook", content=body, headers=headers)
        retry = await client.post(f"/api/channel/whatsapp/{config.agent_id}/webhook", content=body, headers=headers)
    # Then all nested events committed exactly once and only the failed event needed preparation on retry.
    assert first.status_code == 503
    assert retry.status_code == 200
    assert prepare_calls == ["hello", "transient", "hello", "hello", "transient"]
    send.assert_not_awaited()
    async with transaction(fixture.db.first):
        state = await fixture.db.first.fetchone(
            "SELECT (SELECT count(*) FROM channel_event_receipts) AS receipts, "
            "(SELECT count(*) FROM chat_messages) AS messages, (SELECT count(*) FROM gateway_messages) AS enqueued, "
            "(SELECT count(*) FROM gateway_report_deliveries) AS intents, "
            "(SELECT quota_messages_used FROM users WHERE id = %(user)s) AS charged",
            {"user": fixture.sender_id},
        )
    assert state == {"receipts": 4, "messages": 4, "enqueued": 4, "intents": 4, "charged": 4}
