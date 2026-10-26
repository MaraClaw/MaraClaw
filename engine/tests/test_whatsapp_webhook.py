import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from tests.channel_submission_fixtures import channel_db, receipt_db, scope_for

__all__ = ["channel_db", "receipt_db"]
from tests.im_webhook_fixtures import AGENT_ID, channel_config, webhook_client

from app.api import whatsapp
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.db.session import transaction
from app.services.channels import dedup, submissions

BODY = b'{"entry":[{"changes":[{"value":{"messages":[{"id":"wa-event","type":"reaction"}]}}]}]}'


def signed_event(timestamp, account="phone-account"):
    message = {
        "id": "native-time",
        "from": "15550001",
        "type": "text",
        "text": {"body": "hello"},
        "timestamp": timestamp,
    }
    value = {"metadata": {"phone_number_id": account}, "messages": [message]}
    body = json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()
    return body, {"x-hub-signature-256": "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()}


@pytest.mark.parametrize(
    "timestamp", [None, "bad", True, 123, 1.5, "-1", "NaN", "9" * 100, "253402300800", "1798761600"]
)
async def test_invalid_provider_time_is_not_evidence_and_exact_replay_cannot_refresh(
    channel_db, monkeypatch, timestamp
):
    fixture = channel_db
    now = datetime(2026, 10, 20, tzinfo=UTC)
    monkeypatch.setattr(whatsapp, "utcnow", lambda: now)
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=fixture.config))
    original = submissions.accept_channel_event

    async def accept(event):
        async with transaction(fixture.db.first):
            return await original(event)

    monkeypatch.setattr(submissions, "accept_channel_event", accept)
    body, headers = signed_event(timestamp)
    async with webhook_client(whatsapp.router) as client:
        first = await client.post(
            f"/api/channel/whatsapp/{fixture.config.agent_id}/webhook", content=body, headers=headers
        )
        now += timedelta(days=120)
        replay = await client.post(
            f"/api/channel/whatsapp/{fixture.config.agent_id}/webhook", content=body, headers=headers
        )
    async with transaction(fixture.db.second):
        evidence = await channel_event_receipt_dao.latest_eligible_timestamp(scope_for(fixture))
        charged = await fixture.db.second.fetchval(
            "SELECT quota_messages_used FROM users WHERE id = %(id)s", {"id": fixture.sender_id}
        )
    assert first.status_code == replay.status_code == 200
    assert evidence is None
    assert charged == 1


@pytest.mark.parametrize("account", [None, "", "different-account"])
async def test_webhook_requires_signed_receiving_account_without_config_fallback(monkeypatch, account):
    config = channel_config("whatsapp")
    monkeypatch.setattr(whatsapp.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    dispatch = AsyncMock()
    monkeypatch.setattr(submissions, "accept_channel_event", dispatch)
    body, headers = signed_event("1792454400", account)
    async with webhook_client(whatsapp.router) as client:
        response = await client.post(f"/api/channel/whatsapp/{AGENT_ID}/webhook", content=body, headers=headers)
    assert response.status_code == 200
    dispatch.assert_not_awaited()


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
