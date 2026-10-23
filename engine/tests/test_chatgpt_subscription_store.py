from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from tests.chatgpt_subscription_fakes import TicketRedis, callback, oauth_store as oauth_store
from tests.test_chatgpt_subscription import FakeChatGPTTransport, _user

from app.services import (
    chatgpt_oauth as oauth,
    chatgpt_subscription_handoff as browser,
    chatgpt_subscription_store as store,
)


async def test_ticket_fake_enforces_nx_expiry_and_atomic_getdel():
    # Given
    redis = TicketRedis()
    await redis.set("ticket", "first", nx=True, ex=600)
    # When / Then
    assert not await redis.set("ticket", "overwrite", nx=True, ex=600)
    assert await redis.getdel("ticket") == "first"
    assert await redis.getdel("ticket") is None
    assert await redis.set("ticket", "next", nx=True, ex=600)
    redis.clock.advance(600)
    assert await redis.get("ticket") is None


@pytest.mark.parametrize("operation", ["set", "get", "getdel"])
async def test_redis_failures_are_closed_before_provider_or_persistence(monkeypatch, oauth_store, operation):
    # Given
    user = _user()
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    persist = AsyncMock()
    monkeypatch.setattr(browser, "persist_subscription_tokens", persist)
    start = None if operation == "set" else await browser.start_chatgpt_subscription_handoff(user, None)
    oauth_store.fail_on.add(operation)
    action = (
        browser.start_chatgpt_subscription_handoff(user, None)
        if start is None
        else browser.complete_chatgpt_subscription_handoff(user, callback(start))
    )
    # When
    with pytest.raises(HTTPException) as exc:
        await action
    # Then
    assert exc.value.status_code == 503
    assert transport.calls == []
    persist.assert_not_awaited()


async def test_status_redis_outage_is_503_not_unknown_expiry(oauth_store):
    # Given
    oauth_store.fail_on.add("get")
    # When
    with pytest.raises(HTTPException) as exc:
        await browser.chatgpt_subscription_status(_user(), "unknown")
    # Then
    assert exc.value.status_code == 503


async def test_result_storage_failure_returns_503_and_never_reuses_consumed_ticket(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    oauth_store.fail_on.add("set")
    # When
    with pytest.raises(HTTPException) as failure:
        await browser.complete_chatgpt_subscription_handoff(user, callback(start, error="access_denied"))
    oauth_store.fail_on.clear()
    with pytest.raises(HTTPException) as replay:
        await browser.complete_chatgpt_subscription_handoff(user, callback(start))
    # Then
    assert failure.value.status_code == 503
    assert replay.value.status_code == 409


async def test_result_expires_after_300_seconds(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    await browser.complete_chatgpt_subscription_handoff(user, callback(start, error="access_denied"))
    # When
    oauth_store.clock.advance(300)
    view = await store.read_attempt(start.attempt_id, user)
    # Then
    assert view is not None
    assert view.result is None


async def test_invalid_redis_record_is_not_treated_as_an_unknown_attempt(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    key = next(key for key in oauth_store.rows if key.endswith(":owner"))
    _, expiry = oauth_store.rows[key]
    oauth_store.rows[key] = "invalid", expiry
    # When
    with pytest.raises(HTTPException) as exc:
        await browser.chatgpt_subscription_status(user, start.attempt_id)
    # Then
    assert exc.value.status_code == 503
