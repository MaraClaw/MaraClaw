from dataclasses import replace
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from fastapi import HTTPException
from tests.chatgpt_subscription_fakes import callback, oauth_store
from tests.test_chatgpt_subscription import _ACCESS, _model, _user
from tests.test_enterprise_transaction_boundaries import owned_pool

__all__ = ["oauth_store", "owned_pool"]

from app.dao.user_dao import user_dao
from app.db import session
from app.db.connection import DbConnection
from app.services import (
    chatgpt_oauth as oauth,
    chatgpt_subscription_handoff as browser,
    chatgpt_subscription_store as store,
)


@pytest.mark.parametrize("has_model", [False, True])
@pytest.mark.parametrize(
    "outcome",
    ["success", "revoked", "identity", "password", "tenant", "stale", "cancel", "failure", "commit", "expiry"],
)
async def test_completion_releases_pool_and_revalidates_before_committed_result(
    monkeypatch, oauth_store, owned_pool, outcome, has_model
):
    # Given
    user = _user()
    model = _model(tenant_id=user.tenant_id)
    fresh = user
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(side_effect=lambda *_args, **_kwargs: fresh))
    snapshot = model if has_model else None
    current = replace(model, label="concurrently-changed") if outcome == "stale" else snapshot
    monkeypatch.setattr(
        browser.llm_model_dao, "get_subscription_for_tenant", AsyncMock(side_effect=[snapshot, current])
    )
    monkeypatch.setattr(browser.llm_model_dao, "get", AsyncMock(return_value=current))
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    before_write_commits = []
    published = []
    real_finish = store.finish

    async def remote(*_args):
        nonlocal fresh
        assert session.get_connection() is None
        assert owned_pool.active == 0
        async with session.connection_ctx():
            pass
        assert user.identity is not None
        if outcome == "revoked":
            fresh = replace(user, is_active=False)
        if outcome == "identity":
            fresh = replace(user, identity=replace(user.identity, is_active=False))
        if outcome == "password":
            fresh = replace(user, identity=replace(user.identity, must_change_password=True))
        if outcome == "tenant":
            monkeypatch.setattr(DbConnection, "fetchone", AsyncMock(return_value={"is_active": False}))
        if outcome == "cancel":
            raise anyio.get_cancelled_exc_class()
        if outcome == "failure":
            raise httpx.ReadTimeout("provider credential and secret prose")
        if outcome == "expiry":
            oauth_store.clock.advance(600)
        if outcome == "commit":
            monkeypatch.setattr(owned_pool.raw, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
        return oauth.interpret_token_response(200, {"access_token": _ACCESS, "refresh_token": "refresh"})

    async def persist(*_args, **_kwargs):
        assert session.get_connection() is not None
        before_write_commits.append(owned_pool.raw.commits)
        view = await store.read_attempt(start.attempt_id, user)
        assert view is not None
        assert view.result is None
        return model

    async def publish(binding, result):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        if result.status == "authorized":
            assert owned_pool.raw.commits > before_write_commits[0]
        published.append(result)
        await real_finish(binding, result)

    monkeypatch.setattr(browser, "exchange_authorization_code", remote)
    save = AsyncMock(side_effect=persist)
    monkeypatch.setattr(browser, "persist_subscription_tokens", save)
    monkeypatch.setattr(store, "finish", publish)
    # When
    if outcome == "cancel":
        with pytest.raises(anyio.get_cancelled_exc_class()):
            await browser.complete_chatgpt_subscription_handoff(user, callback(start))
    elif outcome == "commit":
        with pytest.raises(RuntimeError, match="commit failed"):
            await browser.complete_chatgpt_subscription_handoff(user, callback(start))
    elif outcome in {"revoked", "identity", "password", "tenant", "stale"}:
        with pytest.raises(HTTPException) as exc:
            await browser.complete_chatgpt_subscription_handoff(user, callback(start))
        assert exc.value.status_code == (409 if outcome == "stale" else 403)
    else:
        result = await browser.complete_chatgpt_subscription_handoff(user, callback(start))
        assert result.status == {"success": "authorized", "failure": "error", "expiry": "expired"}[outcome]
    # Then
    assert len(published) == 1
    assert (published[0].status == "authorized") == (outcome == "success")
    assert "secret prose" not in published[0].model_dump_json()
    if outcome not in {"success", "commit"}:
        save.assert_not_awaited()
    assert await store.pending_ticket(start.attempt_id) is None
    assert owned_pool.active == 0


@pytest.mark.parametrize("change", ["role", "active", "identity", "password", "tenant"])
async def test_start_requires_fresh_active_admin_identity_and_tenant(monkeypatch, oauth_store, owned_pool, change):
    # Given
    user = _user()
    assert user.identity is not None
    fresh = {
        "role": replace(user, role="member"),
        "active": replace(user, is_active=False),
        "identity": replace(user, identity=replace(user.identity, is_active=False)),
        "password": replace(user, identity=replace(user.identity, must_change_password=True)),
        "tenant": user,
    }[change]
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=fresh))
    if change == "tenant":
        monkeypatch.setattr(DbConnection, "fetchone", AsyncMock(return_value={"is_active": False}))
    # When
    with pytest.raises(HTTPException) as exc:
        await browser.start_chatgpt_subscription_handoff(user, None)
    # Then
    assert exc.value.status_code == 403
    assert oauth_store.rows == {}


async def test_actual_cancellation_consumes_ticket_and_shields_terminal_result(monkeypatch, oauth_store):
    # Given
    user = _user()
    start = await browser.start_chatgpt_subscription_handoff(user, None)
    entered = anyio.Event()
    save = AsyncMock()

    async def remote(*_args):
        entered.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(browser, "exchange_authorization_code", remote)
    monkeypatch.setattr(browser, "persist_subscription_tokens", save)
    # When
    with anyio.fail_after(3):
        async with anyio.create_task_group() as group:
            group.start_soon(browser.complete_chatgpt_subscription_handoff, user, callback(start))
            await entered.wait()
            group.cancel_scope.cancel()
    # Then
    result = await browser.chatgpt_subscription_status(user, start.attempt_id)
    assert result.status == "error"
    save.assert_not_awaited()
    with pytest.raises(HTTPException) as exc:
        await browser.complete_chatgpt_subscription_handoff(user, callback(start))
    assert exc.value.status_code == 409
