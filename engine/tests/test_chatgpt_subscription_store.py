from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from tests.chatgpt_subscription_fakes import TicketRedis, callback_query, oauth_store, state_of

__all__ = ["oauth_store"]
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


async def test_state_index_is_keyed_by_the_state_hash(oauth_store):
    # Given
    start = await browser.start_chatgpt_subscription_handoff(_user(), None)
    state = state_of(start)
    # When
    index = oauth_store.rows.get(store._key(state, "state"))
    # Then
    assert index is not None
    assert index[0] == start.attempt_id
    assert not any(state in key for key in oauth_store.rows)


@pytest.mark.parametrize("operation", ["set", "getdel"])
async def test_redis_failures_are_closed_before_provider_or_persistence(monkeypatch, oauth_store, operation):
    # Given
    user = _user()
    transport = FakeChatGPTTransport()
    oauth.set_chatgpt_oauth_transport(transport)
    persist = AsyncMock()
    monkeypatch.setattr(browser, "persist_subscription_tokens", persist)
    start = None if operation == "set" else await browser.start_chatgpt_subscription_handoff(user, None)
    oauth_store.fail_on.add(operation)
    # When / Then
    if start is None:
        with pytest.raises(HTTPException) as exc:
            await browser.start_chatgpt_subscription_handoff(user, None)
        assert exc.value.status_code == 503
    else:
        assert await browser.complete_chatgpt_subscription_callback(callback_query(start)) == "unavailable"
        assert oauth_store.pending(start.attempt_id) is not None
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


async def test_result_storage_failure_is_unavailable_and_never_reuses_consumed_ticket(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    oauth_store.fail_on.add("set")
    # When
    failure = await browser.complete_chatgpt_subscription_callback(callback_query(start, error="access_denied"))
    oauth_store.fail_on.clear()
    replay = await browser.complete_chatgpt_subscription_callback(callback_query(start))
    # Then
    assert failure == "unavailable"
    assert replay == "rejected"


async def test_result_expires_after_300_seconds(oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    await browser.complete_chatgpt_subscription_callback(callback_query(start, error="access_denied"))
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


@pytest.mark.parametrize("corruption", ["invalid_ticket", "foreign_ticket"])
async def test_corrupt_pending_record_fails_the_callback_closed(monkeypatch, oauth_store, corruption):
    # Given
    user = _user()
    first = await browser.start_chatgpt_subscription_handoff(user, None)
    second = await browser.start_chatgpt_subscription_handoff(user, None)
    exchange = AsyncMock()
    monkeypatch.setattr(browser, "exchange_authorization_code", exchange)
    if corruption == "invalid_ticket":
        key = store._key(first.attempt_id, "pending")
        _, expiry = oauth_store.rows[key]
        oauth_store.rows[key] = "invalid", expiry
    else:
        key = store._key(state_of(first), "state")
        _, expiry = oauth_store.rows[key]
        oauth_store.rows[key] = second.attempt_id, expiry
    # When
    outcome = await browser.complete_chatgpt_subscription_callback(callback_query(first))
    # Then
    assert outcome == "unavailable"
    exchange.assert_not_awaited()
