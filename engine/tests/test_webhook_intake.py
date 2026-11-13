import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from tests.test_webhooks_api import MINTED_TOKEN, RecordingRedis
from tests.webhook_intake_fixtures import ASGIInput, drive_asgi

from app.api import webhooks

LEGACY_TOKEN = "valid_token"


@pytest.fixture
def intake_app() -> FastAPI:
    app = FastAPI()
    app.include_router(webhooks.router)
    return app


@pytest.fixture
async def intake_client(intake_app: FastAPI):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=intake_app), base_url="http://test") as client:
        yield client


def _guarded(message: str) -> AsyncMock:
    return AsyncMock(side_effect=AssertionError(message))


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "a" * 10,
        "a" * 12,
        "a" * 21,
        "a" * 23,
        "a" * 64,
        "bad.token11",
        "a" * 10 + "é",
        "a" * 21 + "é",
        "a" * 22 + "=",
    ],
)
async def test_malformed_token_is_answered_before_body_redis_or_sql(monkeypatch, intake_app, token):
    # Given: tripwires on every boundary a malformed token must never reach.
    tripwire = _guarded("Malformed token reached a guarded boundary")
    monkeypatch.setattr(webhooks, "get_redis", tripwire)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", tripwire)
    monkeypatch.setattr(webhooks.agent_dao, "get", tripwire)
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", tripwire)

    # When: the real route receives a request whose body would be readable.
    response = await drive_asgi(intake_app, ASGIInput(f"/api/webhooks/t/{token}", (b"{}",)))

    # Then: the generic answer arrives without a single body chunk read.
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True}
    assert response.reads == 0
    tripwire.assert_not_called()


@pytest.mark.parametrize("token", [LEGACY_TOKEN, MINTED_TOKEN])
async def test_unknown_well_formed_token_costs_one_lookup_and_nothing_else(monkeypatch, intake_app, token):
    # Given: a well-formed token that matches no enabled webhook trigger.
    lookup = AsyncMock(return_value=None)
    tripwire = _guarded("Unknown token reached a guarded boundary")
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks, "get_redis", tripwire)
    monkeypatch.setattr(webhooks.agent_dao, "get", tripwire)
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", tripwire)

    # When: the real route receives it with a readable body.
    response = await drive_asgi(intake_app, ASGIInput(f"/api/webhooks/t/{token}", (b"{}",)))

    # Then: exactly one lookup ran; no body was read and nothing reached Redis, the agent row or the queue.
    assert response.status == 200
    assert json.loads(response.body) == {"ok": True}
    assert response.reads == 0
    lookup.assert_awaited_once_with(token)
    tripwire.assert_not_called()


@pytest.mark.parametrize(
    ("hits", "limit", "status"),
    [(1, 5, 200), (5, 5, 200), (6, 5, 429), (59, 100, 200), (60, 100, 429), (1, 0, 200), (6, 0, 429)],
)
async def test_bounded_intake_preserves_hard_and_per_agent_limits(monkeypatch, intake_client, hits, limit, status):
    # Given: a known trigger, configured agent budget, and current rolling hit count.
    trigger = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="trigger", config={})
    lookup = AsyncMock(return_value=trigger)
    record = AsyncMock(return_value=hits)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=limit)))
    monkeypatch.setattr(webhooks, "_record_and_count_hits", record)
    monkeypatch.setattr(webhooks, "write_audit_log", AsyncMock())
    read_body = AsyncMock(wraps=webhooks.read_bounded_body)
    monkeypatch.setattr(webhooks, "read_bounded_body", read_body)
    enqueue = AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), True))
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", enqueue)

    # When: the request is counted under its trigger before any body read.
    response = await intake_client.post(f"/api/webhooks/t/{LEGACY_TOKEN}", content=b"{}")

    # Then: existing inclusive agent budgets and the hard 60-hit ceiling still apply, and a throttled call reads nothing.
    assert response.status_code == status
    assert response.json() == {"ok": True}
    assert enqueue.await_count == int(status == 200)
    assert read_body.await_count == int(status == 200)
    lookup.assert_awaited_once_with(LEGACY_TOKEN)
    record.assert_awaited_once_with(trigger.id)


@pytest.mark.parametrize("token", [LEGACY_TOKEN, MINTED_TOKEN])
async def test_rate_limit_key_comes_from_the_trigger_not_the_caller(monkeypatch, intake_app, token):
    # Given: a known trigger and a Redis fake that records every key it is asked to touch.
    trigger = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="trigger", config={})
    redis = RecordingRedis()
    monkeypatch.setattr(webhooks, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=trigger))
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=5)))
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", AsyncMock(return_value=(SimpleNamespace(), True)))

    # When: callers with different peer addresses and forwarded-for headers hit the same trigger.
    for peer, forwarded in (("203.0.113.7", "198.51.100.1"), ("203.0.113.8", "198.51.100.2, 192.0.2.9")):
        transport = httpx.ASGITransport(app=intake_app, client=(peer, 4000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                f"/api/webhooks/t/{token}", content=b"{}", headers={"x-forwarded-for": forwarded}
            )
        assert response.status_code == 200

    # Then: only the trigger's own key is used; neither the token, the peer nor the header leaks into it.
    assert set(redis.pipeline_instance.keys) == {f"webhook:rate:{trigger.id}"}


async def test_oversized_body_on_known_trigger_is_413_after_lookup_and_rate_limit(monkeypatch, intake_client):
    # Given: a known trigger whose lookup, rate-limit count and body read report into one ordered log.
    calls: list[str] = []
    trigger = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="trigger", config={})
    real_read = webhooks.read_bounded_body

    async def lookup(token):
        calls.append("lookup")
        return trigger

    async def record(trigger_id):
        calls.append("rate")
        return 1

    async def read(request, limit):
        calls.append("body")
        return await real_read(request, limit)

    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks, "_record_and_count_hits", record)
    monkeypatch.setattr(webhooks, "read_bounded_body", read)
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=5)))
    enqueue = _guarded("Oversized body reached the queue")
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", enqueue)

    # When: the body exceeds the 64 KiB cap.
    response = await intake_client.post(
        f"/api/webhooks/t/{LEGACY_TOKEN}", content=b"x" * (webhooks.MAX_PAYLOAD_SIZE + 1)
    )

    # Then: the known trigger is resolved and counted first, and the oversize still answers 413.
    assert response.status_code == 413
    assert response.json() == {"ok": True}
    assert calls == ["lookup", "rate", "body"]
    enqueue.assert_not_called()


@pytest.mark.parametrize("signature", ["valid", "missing", "forged", "non_ascii"])
async def test_generic_signature_rejects_bad_headers_without_leaking_or_enqueuing(
    monkeypatch, intake_client, signature
):
    # Given: a secret-bearing trigger and a body at the existing 64 KiB boundary.
    body = b"{}" + b" " * (65536 - 2)
    trigger = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="trigger", config={"secret": "secret"})
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=trigger))
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=5)))
    monkeypatch.setattr(webhooks, "_record_and_count_hits", AsyncMock(return_value=1))
    enqueue = AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), True))
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", enqueue)
    digest = b"sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest().encode()
    values = {"valid": digest, "missing": b"", "forged": b"sha256=forged", "non_ascii": b"sha256=\xff"}

    # When: authenticating a real HTTP request with the supplied signature bytes.
    response = await intake_client.post(
        f"/api/webhooks/t/{LEGACY_TOKEN}", content=body, headers=[(b"x-hub-signature-256", values[signature])]
    )

    # Then: only the authentic boundary-size request is accepted, with generic responses for all.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert enqueue.await_count == int(signature == "valid")
    if signature == "valid":
        call = enqueue.await_args
        assert call is not None
        assert call.kwargs["body"] == body


async def test_malformed_unknown_and_forged_requests_are_indistinguishable(monkeypatch, intake_client):
    # Given: one known signed trigger; every other token is unknown or malformed.
    trigger = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="trigger", config={"secret": "secret"})

    async def lookup(token):
        return trigger if token == LEGACY_TOKEN else None

    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=5)))
    monkeypatch.setattr(webhooks, "_record_and_count_hits", AsyncMock(return_value=1))
    enqueue = _guarded("A rejected request reached the queue")
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", enqueue)

    # When: a malformed token, an unknown token and a forged signature arrive with the same body.
    responses = [
        await intake_client.post("/api/webhooks/t/short", content=b"{}"),
        await intake_client.post(f"/api/webhooks/t/{MINTED_TOKEN}", content=b"{}"),
        await intake_client.post(
            f"/api/webhooks/t/{LEGACY_TOKEN}", content=b"{}", headers={"x-hub-signature-256": "sha256=forged"}
        ),
    ]

    # Then: status, body and framing headers are identical, so none reveals which check failed.
    shapes = {(r.status_code, r.content, r.headers["content-type"], r.headers["content-length"]) for r in responses}
    assert shapes == {(200, b'{"ok":true}', "application/json", "11")}
    enqueue.assert_not_called()
