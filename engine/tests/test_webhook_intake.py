import hashlib
import hmac
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from tests.test_webhooks_api import RecordingRedis

from app.api import webhooks


@pytest.fixture
async def intake_client():
    app = FastAPI()
    app.include_router(webhooks.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    ("token", "plausible"),
    [
        ("valid_token", True),
        ("ABcd12_-xyz", True),
        ("short", False),
        ("a" * 10, False),
        ("a" * 12, False),
        ("bad.token11", False),
        ("a" * 10 + "é", False),
    ],
)
async def test_unknown_tokens_keep_generic_response_and_only_plausible_tokens_use_redis(
    monkeypatch, intake_client, token, plausible
):
    # Given: no matching trigger and a recording Redis pipeline.
    redis = AsyncMock(return_value=RecordingRedis())
    monkeypatch.setattr(webhooks, "get_redis", redis)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=None))
    enqueue = AsyncMock(side_effect=AssertionError("Unknown token must not enqueue"))
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", enqueue)

    # When: sending a bounded request to an unknown token.
    response = await intake_client.post(f"/api/webhooks/t/{token}", content=b"{}")

    # Then: token existence is hidden, without allocating Redis keys for malformed tokens.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert redis.await_count == int(plausible)
    enqueue.assert_not_called()


@pytest.mark.parametrize(
    ("hits", "limit", "status"),
    [(1, 5, 200), (5, 5, 200), (6, 5, 429), (59, 100, 200), (60, 100, 429), (1, 0, 200), (6, 0, 429)],
)
async def test_bounded_intake_preserves_hard_and_per_agent_limits(monkeypatch, intake_client, hits, limit, status):
    # Given: a known trigger, configured agent budget, and current rolling hit count.
    trigger = SimpleNamespace(id=uuid.uuid4(), agent_id=uuid.uuid4(), name="trigger", config={})
    lookup = AsyncMock(return_value=trigger)
    monkeypatch.setattr(webhooks.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(webhook_rate_limit=limit)))
    monkeypatch.setattr(webhooks, "_record_and_count_hits", AsyncMock(return_value=hits))
    monkeypatch.setattr(webhooks, "write_audit_log", AsyncMock())
    enqueue = AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), True))
    monkeypatch.setattr(webhooks, "enqueue_webhook_execution", enqueue)

    # When: the request reaches rate limiting after its bounded read.
    response = await intake_client.post("/api/webhooks/t/valid_token", content=b"{}")

    # Then: existing inclusive agent budgets and the hard 60-hit ceiling still apply.
    assert response.status_code == status
    assert response.json() == {"ok": True}
    assert enqueue.await_count == int(status == 200)
    assert lookup.await_count == int(hits < 60)


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
        "/api/webhooks/t/valid_token", content=body, headers=[(b"x-hub-signature-256", values[signature])]
    )

    # Then: only the authentic boundary-size request is accepted, with generic responses for all.
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert enqueue.await_count == int(signature == "valid")
    if signature == "valid":
        call = enqueue.await_args
        assert call is not None
        assert call.kwargs["body"] == body
