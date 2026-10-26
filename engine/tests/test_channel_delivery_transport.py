import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core.json_types import json_object_from_response
from app.dao.channel_event_receipt_dao import channel_event_receipt_dao
from app.records.channel_config import ChannelConfigRecord
from app.records.outbound_plan import OutboundChunk, OutboundProvider, OutboundTarget
from app.services import gateway_report_relay as relay
from app.services.channels import google_chat, outbound_transport
from app.services.channels.outbound_errors import ProviderDeliveryError, ProviderPayloadRejectedError, retry_at
from app.services.channels.outbound_plan import ConfiguredOutbound
from app.services.channels.whatsapp_window import WhatsAppReplyWindowError
from gateway_report_relay_fakes import RelayStore


def transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def configured(provider: OutboundProvider) -> ConfiguredOutbound:
    config = ChannelConfigRecord(
        uuid4(),
        uuid4(),
        app_id="application",
        app_secret="live-secret",
        extra_config={
            "service_account_json": {"client_email": "bot@example.test", "private_key": "private-key"},
        },
    )
    target = OutboundTarget(
        provider=provider,
        config_id=config.id,
        account=("application",),
        destination="destination",
        recipient_type="open_id",
        endpoint="v23.0",
    )
    return ConfiguredOutbound(target, config)


@pytest.mark.parametrize("provider", ["discord", "google_chat", "feishu", "slack", "whatsapp"])
async def test_transport_sends_exact_chunk_with_only_supported_stable_keys(monkeypatch, provider: OutboundProvider):
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        if "app_access_token" in request.url.path:
            return httpx.Response(200, json={"code": 0, "app_access_token": "fresh-token"})
        return httpx.Response(
            200,
            json={
                "id": "message",
                "name": "spaces/message",
                "ok": True,
                "messages": [{"id": "message"}],
                "code": 0,
                "data": {"message_id": "message"},
            },
        )

    async def token(*_args, **_kwargs):
        return "fresh-google-token"

    transport(monkeypatch, respond)
    monkeypatch.setattr(google_chat, "_service_account_access_token", token)
    outbound = configured(provider)
    if provider == "whatsapp":
        monkeypatch.setattr(
            channel_event_receipt_dao, "latest_eligible_timestamp", AsyncMock(return_value=datetime.now(UTC))
        )
    chunk = OutboundChunk(text="  exact\n世界  ", key=uuid4())

    await outbound_transport.send_chunk(outbound, chunk)
    await outbound_transport.send_chunk(outbound, chunk)

    sent = [request for request in requests if "app_access_token" not in request.url.path]
    bodies = [json.loads(request.content) for request in sent]
    assert len(sent) == 2
    assert bodies[0] == bodies[1]
    match provider:
        case "discord":
            assert bodies[0] == {"content": chunk.text, "nonce": chunk.key.hex[:25], "enforce_nonce": True}
            assert len(bodies[0]["nonce"]) <= 25
        case "google_chat":
            assert sent[0].url.params["requestId"] == sent[1].url.params["requestId"] == str(chunk.key)
            assert bodies[0] == {"text": chunk.text}
        case "feishu":
            assert bodies[0]["uuid"] == str(chunk.key)
            assert json.loads(bodies[0]["content"])["text"] == chunk.text
        case "slack":
            assert bodies[0] == {"channel": "destination", "text": chunk.text}
        case "whatsapp":
            assert bodies[0] == {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": "destination",
                "type": "text",
                "text": {"preview_url": False, "body": chunk.text},
            }


@pytest.mark.parametrize(
    ("age", "reason"),
    [
        (None, "whatsapp_window_unavailable"),
        (timedelta(microseconds=-1), "whatsapp_window_unavailable"),
        (timedelta(), None),
        (timedelta(hours=24) - timedelta(microseconds=1), None),
        (timedelta(hours=24), "whatsapp_window_expired"),
        (timedelta(hours=24, microseconds=1), "whatsapp_window_expired"),
    ],
)
async def test_whatsapp_checks_strict_window_at_actual_transport_boundary(monkeypatch, age, reason):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    timestamp = now - age if age is not None else None
    lookup = AsyncMock(return_value=timestamp)
    monkeypatch.setattr(channel_event_receipt_dao, "latest_eligible_timestamp", lookup)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "ack"}]})

    transport(monkeypatch, respond)
    if reason is not None:
        with pytest.raises(WhatsAppReplyWindowError) as failure:
            await outbound_transport.send_chunk(
                configured("whatsapp"), OutboundChunk(text="reply", key=uuid4()), clock=lambda: now
            )
        assert failure.value.reason == reason
    else:
        await outbound_transport.send_chunk(
            configured("whatsapp"), OutboundChunk(text="reply", key=uuid4()), clock=lambda: now
        )
    assert len(requests) == (1 if reason is None else 0)
    lookup.assert_awaited_once()


@pytest.mark.parametrize("kind", ["seconds", "http_date", "discord_body"])
async def test_provider_deadline_longer_than_local_cap_is_preserved(monkeypatch, kind):
    store = RelayStore()
    store.install(monkeypatch)
    row = store.intent("im")
    row.attempts = 10000
    deadline = store.now + timedelta(seconds=900)
    headers = {"Retry-After": "900" if kind == "seconds" else format_datetime(deadline, usegmt=True)}
    if kind == "discord_body":
        store.session.source_channel = "discord"
        headers = {"Retry-After": "10"}

    def respond(_request):
        assert not store.transaction.get()
        return httpx.Response(429, headers=headers, json={"retry_after": 900})

    transport(monkeypatch, respond)
    monkeypatch.setattr(relay, "send_chunk", outbound_transport.send_chunk)

    await relay.relay_batch(store.clock)

    assert store.rows[row.id].available_at == deadline
    assert store.rows[row.id].next_chunk == 0
    assert store.rows[row.id].terminal_at is None


@pytest.mark.parametrize(
    ("status", "body", "terminal"),
    [
        (200, {"ok": False, "error": "msg_too_long"}, True),
        (200, {"ok": False, "error": "invalid_auth"}, False),
        (200, {"ok": False, "error": "missing_scope"}, False),
        (400, {"ok": False, "error": "invalid_arguments"}, False),
        (401, {"error": "invalid_auth"}, False),
        (403, {"error": "missing_scope"}, False),
        (503, {"error": "service_unavailable"}, False),
    ],
)
async def test_http_and_application_errors_are_conservative(monkeypatch, status, body, terminal):
    transport(monkeypatch, lambda _request: httpx.Response(status, json=body))

    with pytest.raises((ProviderDeliveryError, httpx.HTTPStatusError)) as failure:
        await outbound_transport.send_chunk(configured("slack"), OutboundChunk(text="text", key=uuid4()))

    assert isinstance(failure.value, ProviderPayloadRejectedError) == terminal


@pytest.mark.parametrize(
    ("body", "terminal"),
    [
        ({"code": 50035, "errors": {"content": {"_errors": [{"code": "BASE_TYPE_MAX_LENGTH"}]}}}, True),
        ({"code": 50035, "errors": {"nonce": {"_errors": [{"code": "INVALID_NONCE"}]}}}, False),
        ({"code": 50013, "message": "Missing Permissions"}, False),
    ],
)
async def test_discord_rejects_only_conclusive_content_errors(monkeypatch, body, terminal):
    transport(monkeypatch, lambda _request: httpx.Response(400, json=body))

    with pytest.raises((ProviderDeliveryError, httpx.HTTPStatusError)) as failure:
        await outbound_transport.send_chunk(configured("discord"), OutboundChunk(text="text", key=uuid4()))

    assert isinstance(failure.value, ProviderPayloadRejectedError) == terminal


@pytest.mark.parametrize("header", ["nonsense", "NaN", "inf", "-1", "1e300"])
def test_invalid_retry_after_is_ignored_without_hiding_http_failure(header):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    response = httpx.Response(
        429,
        headers={"Retry-After": header},
        json={},
        request=httpx.Request("POST", "https://slack.com/api/chat.postMessage"),
    )

    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()

    assert retry_at(failure.value, now) is None
    assert json_object_from_response(response) == {}


@pytest.mark.parametrize("delay", [True, -1, "900", 10**400])
def test_invalid_discord_retry_delay_is_ignored_without_hiding_http_failure(delay):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    response = httpx.Response(
        429,
        json={"retry_after": delay},
        request=httpx.Request("POST", "https://discord.com/api/v10/channels/1/messages"),
    )

    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()

    assert retry_at(failure.value, now) is None
