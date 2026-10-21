import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from tests.transaction_pool_fakes import CountingPool

from app.api import auth
from app.api.background_tasks import drain_background_tasks, start_background_tasks
from app.config import get_settings
from app.db import session
from app.records.identity import IdentityProviderRecord, IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import ForgotPasswordRequest, IdentityBindRequest, OAuthCallbackRequest, UserLogin
from app.services import password_reset_service, system_email_service
from app.services.auth_registry import auth_provider_registry
from app.services.email_verification_service import email_verification_service
from app.services.sso_service import sso_service


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_callback_failure_rolls_back_owned_scope(monkeypatch, failure):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setattr(auth.identity_provider_dao, "get_preferred", AsyncMock(return_value=None))
    committed = []

    async def exchange(*args):
        assert pool.active == 0
        return {"access_token": "remote"}

    async def persist(*args, **kwargs):
        assert session.get_connection() is not None
        session.run_after_commit(lambda: committed.append("identity and membership"))
        raise failure()

    provider = SimpleNamespace(
        provider=None,
        config={},
        exchange_code_for_token=exchange,
        get_user_info=AsyncMock(return_value=SimpleNamespace()),
        find_or_create_user=persist,
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=provider))
    data = OAuthCallbackRequest(code="code", state="state", redirect_uri="")
    with pytest.raises(failure):
        await auth.oauth_callback("google", data)
    assert pool.raw.rollbacks == 1
    assert committed == []
    assert pool.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_reset_email_waits_for_owning_commit(monkeypatch, failure):
    start_background_tasks()
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    identity = SimpleNamespace(id=uuid4(), email="member@example.com", is_active=True)
    monkeypatch.setattr(auth.identity_dao, "get_by_email", AsyncMock(return_value=identity))
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        password_reset_service,
        "create_password_reset_token",
        AsyncMock(return_value=("token", datetime.now(UTC) + timedelta(minutes=30))),
    )
    monkeypatch.setattr(password_reset_service, "build_password_reset_url", AsyncMock(return_value="https://app/reset"))
    send = AsyncMock()
    monkeypatch.setattr(system_email_service, "send_password_reset_email", send)
    tasks = BackgroundTasks()

    async def run():
        async with session.connection_ctx():
            await auth.forgot_password(
                ForgotPasswordRequest(email=identity.email), tasks, Request({"type": "http", "headers": []})
            )
            send.assert_not_awaited()
            assert tasks.tasks == []
            if failure is not None:
                raise failure()

    if failure is None:
        await run()
    else:
        with pytest.raises(failure):
            await run()
    await drain_background_tasks()
    start_background_tasks()
    assert send.await_count == (1 if failure is None else 0)
    assert pool.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["membership", "identity", "tenant_drift", "identity_drift", "password"])
async def test_bind_rejects_revoked_actor_after_remote_exchange(monkeypatch, revocation):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    identity = IdentityRecord(id=uuid4())
    actor = UserRecord(id=uuid4(), identity_id=identity.id)
    fresh = replace(actor, identity=identity)
    if revocation == "membership":
        fresh.is_active = False
    elif revocation == "identity":
        identity.is_active = False
    elif revocation == "tenant_drift":
        fresh.tenant_id = uuid4()
    elif revocation == "identity_drift":
        fresh.identity_id = uuid4()
    else:
        identity.must_change_password = True
    provider = SimpleNamespace(
        provider=None,
        config={},
        exchange_code_for_token=AsyncMock(return_value={"access_token": "remote"}),
        get_user_info=AsyncMock(return_value=SimpleNamespace(provider_user_id="external", raw_data={})),
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(auth.user_dao, "get_with_identity", AsyncMock(return_value=fresh))
    monkeypatch.setattr(sso_service, "check_duplicate_identity", AsyncMock(return_value=None))
    link = AsyncMock()
    monkeypatch.setattr(sso_service, "link_identity", link)
    with pytest.raises(HTTPException) as error:
        await auth.bind_identity("google", IdentityBindRequest(provider_type="google", code="code"), actor)
    assert error.value.status_code == 403
    link.assert_not_awaited()
    assert pool.raw.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_verification_email_waits_for_owning_commit(monkeypatch, failure):
    start_background_tasks()
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    identity = IdentityRecord(id=uuid4(), email="member@example.com")
    user = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity)
    monkeypatch.setattr(auth.identity_dao, "get", AsyncMock(return_value=identity))
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        email_verification_service,
        "create_email_verification_token",
        AsyncMock(return_value=("code", datetime.now(UTC) + timedelta(minutes=30))),
    )
    monkeypatch.setattr("app.services.frontend_origin.resolve_frontend_base_url", AsyncMock(return_value="https://app"))
    monkeypatch.setattr(
        email_verification_service, "build_email_verification_url", AsyncMock(return_value="https://app/verify")
    )
    send = AsyncMock()
    monkeypatch.setattr(email_verification_service, "send_verification_email", send)
    tasks = BackgroundTasks()

    async def run():
        async with session.connection_ctx():
            await auth._send_verification_email_task(user, tasks, get_settings())
            send.assert_not_awaited()
            assert tasks.tasks == []
            if failure is not None:
                raise failure()

    if failure is None:
        await run()
    else:
        with pytest.raises(failure):
            await run()
    await drain_background_tasks()
    start_background_tasks()
    assert send.await_count == (1 if failure is None else 0)
    assert pool.active == 0


@pytest.mark.asyncio
async def test_unverified_login_commits_email_before_403(monkeypatch):
    start_background_tasks()
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    identity = IdentityRecord(id=uuid4(), email="member@example.com", password_hash="hash")
    user = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity)
    monkeypatch.setattr(auth.identity_dao, "get_by_login_identifier", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth, "verify_password_async", AsyncMock(return_value=True))
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(auth.user_dao, "get_representative_user_for_identity", AsyncMock(return_value=user))
    deliveries = []

    async def queue(*args):
        session.run_after_commit(lambda: deliveries.append("verification"))

    monkeypatch.setattr(auth, "_send_verification_email_task", queue)
    with pytest.raises(HTTPException) as error:
        await auth.login(
            UserLogin(login_identifier=identity.email or "", password="secret"),
            BackgroundTasks(),
            Request({"type": "http", "headers": []}),
        )
    assert error.value.status_code == 403
    assert deliveries == ["verification"]
    assert pool.raw.commits == 1
    assert pool.raw.rollbacks == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["inactive", "config", "config_in_place", "replacement", "removed", "sso"])
async def test_http_callback_rejects_changed_provider_before_writes(monkeypatch, revocation):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    previous = IdentityProviderRecord(id=uuid4(), provider_type="google", name="Google", config={"client_id": "before"})
    fresh = replace(previous)
    if revocation == "inactive":
        fresh.is_active = False
    elif revocation == "config":
        fresh.config = {"client_id": "after"}
    elif revocation == "replacement":
        fresh.id = uuid4()
    elif revocation == "sso":
        fresh.sso_login_enabled = not previous.sso_login_enabled
    monkeypatch.setattr(
        auth.identity_provider_dao, "get_preferred", AsyncMock(return_value=None if revocation == "removed" else fresh)
    )

    async def exchange(*args):
        assert pool.active == 0
        if revocation == "config_in_place":
            previous.config["client_id"] = "after"
        return {"access_token": "remote"}

    persist = AsyncMock()
    provider = SimpleNamespace(
        provider=previous,
        config=previous.config,
        exchange_code_for_token=exchange,
        get_user_info=AsyncMock(return_value=SimpleNamespace()),
        find_or_create_user=persist,
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=provider))
    app = FastAPI()
    app.include_router(auth.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/auth/google/callback", json={"code": "code", "state": "state", "redirect_uri": ""}
        )
    assert response.status_code == 403
    persist.assert_not_awaited()
    assert pool.active == 0
    assert pool.raw.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("is_new", [True, False])
async def test_http_callback_preserves_unverified_identity_login(monkeypatch, is_new):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    identity = IdentityRecord(id=uuid4(), email="member@example.com", email_verified=False)
    user = UserRecord(id=uuid4(), identity_id=identity.id, identity=identity, created_at=datetime.now(UTC))
    monkeypatch.setattr(auth.identity_provider_dao, "get_preferred", AsyncMock(return_value=None))
    monkeypatch.setattr(auth.user_dao, "get_with_identity", AsyncMock(return_value=user))
    monkeypatch.setattr(auth.user_dao, "get_by_identity_id", AsyncMock(return_value=[user]))

    async def remote(*args):
        assert pool.active == 0
        return {"access_token": "remote"}

    async def persist(*args):
        assert session.get_connection() is not None
        return user, is_new

    provider = SimpleNamespace(
        provider=None,
        config={},
        exchange_code_for_token=remote,
        get_user_info=AsyncMock(return_value=SimpleNamespace()),
        find_or_create_user=persist,
    )
    monkeypatch.setattr(auth_provider_registry, "get_provider", AsyncMock(return_value=provider))
    app = FastAPI()
    app.include_router(auth.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/auth/google/callback", json={"code": "code", "state": "state", "redirect_uri": ""}
        )
    assert response.status_code == 200
    assert response.json()["user"]["id"] == str(user.id)
    assert response.json()["access_token"]
    assert pool.active == 0
