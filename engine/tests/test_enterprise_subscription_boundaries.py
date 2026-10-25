import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from tests.test_enterprise_transaction_boundaries import _actor, _model, owned_pool

__all__ = ["owned_pool"]

from app.dao.user_dao import user_dao
from app.db import session
from app.services import enterprise_llm, grok_subscription, openclaw_hot_cache


async def test_activation_rollback_preserves_model_cache(monkeypatch, owned_pool):
    model = _model()
    cache_key = "bundle:enterprise-boundary-test"
    openclaw_hot_cache.cache_set(cache_key, "cached-model")
    monkeypatch.setattr(enterprise_llm.tenant_dao, "get", AsyncMock(return_value=None))

    async def activate_then_fail():
        async with session.connection_ctx():
            await enterprise_llm.activate_pool_model_for_tenant(model)
            raise RuntimeError("rollback")

    try:
        with pytest.raises(RuntimeError, match="rollback"):
            await activate_then_fail()
        assert openclaw_hot_cache.cache_get(cache_key) == "cached-model"
    finally:
        openclaw_hot_cache.cache_drop(cache_key)


@pytest.mark.parametrize("service", [grok_subscription])
@pytest.mark.parametrize("has_model", [False, True])
@pytest.mark.parametrize("outcome", ["revoked", "stale", "cancel", "failure", "success"])
async def test_device_poll_releases_pool_and_publishes_only_after_commit(
    monkeypatch, owned_pool, service, outcome, has_model
):
    model = _model()
    user = _actor(model)
    identity = user.identity
    assert identity is not None
    expires = datetime.now(UTC) + timedelta(minutes=10)
    common = {
        "session_id": "boundary-test",
        "tenant_id": model.tenant_id,
        "user_code": "CODE",
        "interval": 5,
        "expires_at": expires,
        "verification_url": "https://auth.invalid/device",
    }
    oauth_session = service.GrokOAuthSession(**common, device_code="device-secret")
    poll_name = "poll_device_token"
    status = service.grok_subscription_status
    monkeypatch.setattr(service, "_SESSIONS", {"boundary-test": oauth_session})
    snapshot = model if has_model else None
    current = SimpleNamespace(id="concurrent-model") if outcome == "stale" else snapshot
    monkeypatch.setattr(
        service.llm_model_dao, "get_subscription_for_tenant", AsyncMock(side_effect=[snapshot, current])
    )
    monkeypatch.setattr(service.llm_model_dao, "get", AsyncMock(return_value=model))
    fresh = replace(user, is_active=False) if outcome == "revoked" else user
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=fresh))

    async def remote(*args, **kwargs):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        async with session.connection_ctx():
            pass
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "failure":
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(status="authorized", interval=5, tokens=SimpleNamespace())

    async def persist(*args, **kwargs):
        assert session.get_connection() is not None
        assert service._SESSIONS["boundary-test"].status == "pending"
        return model

    monkeypatch.setattr(service, poll_name, remote)
    save = AsyncMock(side_effect=persist)
    monkeypatch.setattr(service, "persist_subscription_tokens", save)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await status(user, "boundary-test")
    elif outcome == "failure":
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await status(user, "boundary-test")
    elif outcome in {"revoked", "stale"}:
        with pytest.raises(HTTPException) as exc:
            await status(user, "boundary-test")
        assert exc.value.status_code == (403 if outcome == "revoked" else 409)
    else:
        result = await status(user, "boundary-test")
        assert result.model_id == model.id
        assert service._SESSIONS["boundary-test"].status == "authorized"
    if outcome != "success":
        save.assert_not_awaited()
        assert service._SESSIONS["boundary-test"].status == "pending"
    assert owned_pool.active == 0


@pytest.mark.parametrize("service", [grok_subscription])
@pytest.mark.parametrize("outcome", ["revoked", "cancel", "failure", "success"])
async def test_device_start_releases_pool_and_rechecks_authority(monkeypatch, owned_pool, service, outcome):
    user = _actor(_model())
    monkeypatch.setattr(service, "_SESSIONS", {})
    fresh = replace(user, role="member") if outcome == "revoked" else user
    monkeypatch.setattr(user_dao, "get_with_identity", AsyncMock(return_value=fresh))

    async def remote():
        assert session.get_connection() is None
        assert owned_pool.active == 0
        async with session.connection_ctx():
            pass
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "failure":
            raise RuntimeError("provider unavailable")
        return SimpleNamespace(
            device_code="private-device",
            device_auth_id="private-device",
            user_code="CODE",
            interval=5,
            verification_url="https://auth.invalid/device",
            expires_in=600,
        )

    monkeypatch.setattr(service, "request_device_code", remote)
    start = grok_subscription.start_grok_subscription_handoff
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await start(user, None)
    elif outcome == "failure":
        with pytest.raises(RuntimeError, match="provider unavailable"):
            await start(user, None)
    elif outcome == "revoked":
        with pytest.raises(HTTPException) as exc:
            await start(user, None)
        assert exc.value.status_code == 403
    else:
        result = await start(user, None)
        assert result.session_id in service._SESSIONS
    if outcome != "success":
        assert service._SESSIONS == {}
    assert owned_pool.active == 0
