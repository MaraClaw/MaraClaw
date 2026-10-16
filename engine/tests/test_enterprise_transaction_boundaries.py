import asyncio
import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from tests.test_enterprise_llm import _model
from tests.transaction_pool_fakes import CountingPool

from app.api import enterprise
from app.db import session
from app.db.connection import DbConnection
from app.records.identity import IdentityRecord
from app.records.user import UserRecord
from app.schemas.schemas import LLMModelCreate
from app.services import chatgpt_subscription, grok_subscription


@pytest.fixture
def owned_pool(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setattr(DbConnection, "execute", AsyncMock())
    monkeypatch.setattr(DbConnection, "fetchone", AsyncMock(return_value={"is_active": True}))
    return pool


async def test_model_creation_and_default_assignment_share_owned_transaction(monkeypatch, owned_pool):
    model = _model()
    user = UserRecord(id=model.id, tenant_id=model.tenant_id, role="org_admin")
    observed = []

    async def create(**kwargs):
        observed.append(session.get_connection())
        return model

    async def fail_assignment(**kwargs):
        observed.append(session.get_connection())
        raise RuntimeError("assignment failed")

    monkeypatch.setattr(enterprise.llm_model_dao, "create", create)
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=SimpleNamespace(default_model_id=None)))
    monkeypatch.setattr(enterprise.tenant_dao, "update", fail_assignment)
    with pytest.raises(RuntimeError, match="assignment failed"):
        await enterprise.add_llm_model(
            LLMModelCreate(
                provider="grok", model="grok", api_key="secret", label="", temperature=0.2, reasoning_effort=None
            ),
            None,
            user,
        )
    assert observed[0] is not None
    assert observed[0] is observed[1]
    assert owned_pool.raw.commits == 0
    assert owned_pool.raw.rollbacks == 1


@pytest.mark.parametrize("service", [grok_subscription, chatgpt_subscription])
async def test_admin_refresh_rechecks_revoked_actor_even_when_credentials_changed(monkeypatch, owned_pool, service):
    kind = "grok_subscription" if service is grok_subscription else "chatgpt_subscription"
    model = _model(auth_kind=kind, refresh_token_encrypted="old-refresh")
    user = UserRecord(id=model.id, tenant_id=model.tenant_id, role="org_admin")
    revoked = replace(user, is_active=False)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def remote(refresh):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        entered.set()
        await release.wait()
        return SimpleNamespace(status="authorized", tokens=SimpleNamespace(refresh_token="rotated"))

    monkeypatch.setattr(service.llm_model_dao, "get", AsyncMock(side_effect=[model, replace(model, label="changed")]))
    monkeypatch.setattr(enterprise.user_dao, "get_with_identity", AsyncMock(return_value=revoked))
    monkeypatch.setattr(service, "refresh_access_token", remote)
    persist = AsyncMock(return_value=model)
    monkeypatch.setattr(service, "persist_subscription_tokens", persist)
    refresh = (
        grok_subscription.refresh_grok_subscription_for_admin
        if service is grok_subscription
        else chatgpt_subscription.refresh_chatgpt_subscription_for_admin
    )
    task = asyncio.create_task(refresh(user, model.id))
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    with pytest.raises(HTTPException) as exc:
        await task
    assert exc.value.status_code in {401, 403}
    persist.assert_not_awaited()


def _actor(model):
    identity = IdentityRecord(id=uuid.uuid4())
    return UserRecord(
        id=uuid.uuid4(), tenant_id=model.tenant_id, role="org_admin", identity_id=identity.id, identity=identity
    )


@pytest.mark.parametrize("service", [grok_subscription, chatgpt_subscription])
@pytest.mark.parametrize("change", ["identity", "membership", "role", "tenant", "password", "model", "credential"])
async def test_refresh_rejects_changed_authority_or_snapshot(monkeypatch, owned_pool, service, change):
    kind = "grok_subscription" if service is grok_subscription else "chatgpt_subscription"
    model = _model(auth_kind=kind, refresh_token_encrypted="old-refresh")
    user = _actor(model)
    assert user.identity is not None
    fresh = user
    current = model
    if change == "identity":
        fresh = replace(user, identity=replace(user.identity, is_active=False))
    elif change == "membership":
        fresh = replace(user, tenant_id=uuid.uuid4())
    elif change == "role":
        fresh = replace(user, role="member")
    elif change == "password":
        fresh = replace(user, identity=replace(user.identity, must_change_password=True))
    elif change == "model":
        current = replace(model, base_url="https://changed.invalid")
    elif change == "credential":
        current = replace(model, refresh_token_encrypted="newer-refresh")
    if change == "tenant":
        monkeypatch.setattr(DbConnection, "fetchone", AsyncMock(return_value={"is_active": False}))
    monkeypatch.setattr(service.llm_model_dao, "get", AsyncMock(side_effect=[model, current]))
    monkeypatch.setattr(enterprise.user_dao, "get_with_identity", AsyncMock(return_value=fresh))

    async def remote(_refresh):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        async with session.connection_ctx():
            pass
        return SimpleNamespace(status="authorized", tokens=SimpleNamespace(refresh_token="rotated"))

    monkeypatch.setattr(service, "refresh_access_token", remote)
    persist = AsyncMock(return_value=model)
    monkeypatch.setattr(service, "persist_subscription_tokens", persist)
    refresh = (
        grok_subscription.refresh_grok_subscription_for_admin
        if service is grok_subscription
        else chatgpt_subscription.refresh_chatgpt_subscription_for_admin
    )
    with pytest.raises(HTTPException) as exc:
        await refresh(user, model.id)
    assert exc.value.status_code == (409 if change in {"model", "credential"} else 403)
    persist.assert_not_awaited()
    assert owned_pool.active == 0


@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_probe_remote_failure_or_cancel_releases_checkout(monkeypatch, owned_pool, failure):
    model = _model()
    user = _actor(model)
    monkeypatch.setattr(enterprise.llm_model_dao, "get", AsyncMock(return_value=model))
    monkeypatch.setattr(enterprise.user_dao, "get_with_identity", AsyncMock(return_value=user))

    async def complete(**kwargs):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        async with session.connection_ctx():
            pass
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("probe failed")

    monkeypatch.setattr(enterprise, "create_llm_client", lambda **kwargs: SimpleNamespace(complete=complete))
    request = enterprise.LLMTestRequest(provider=model.provider, model=model.model, model_id=str(model.id))
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await enterprise.probe_llm_model(request, user)
    else:
        result = await enterprise.probe_llm_model(request, user)
        assert result["success"] is False
    assert owned_pool.active == 0


async def test_probe_rejects_authority_revoked_during_completion(monkeypatch, owned_pool):
    model = _model()
    user = _actor(model)
    monkeypatch.setattr(enterprise.llm_model_dao, "get", AsyncMock(return_value=model))
    monkeypatch.setattr(enterprise.user_dao, "get_with_identity", AsyncMock(return_value=replace(user, role="member")))

    async def complete(**kwargs):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(enterprise, "create_llm_client", lambda **kwargs: SimpleNamespace(complete=complete))
    with pytest.raises(HTTPException) as exc:
        await enterprise.probe_llm_model(
            enterprise.LLMTestRequest(provider=model.provider, model=model.model, model_id=str(model.id)), user
        )
    assert exc.value.status_code == 403


async def test_invitation_batch_failure_discards_email_effects(monkeypatch, owned_pool):
    model = _model()
    user = _actor(model)
    background = BackgroundTasks()
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=SimpleNamespace(name="Company")))
    monkeypatch.setattr(enterprise, "_ensure_invitation_email_enabled", AsyncMock())
    monkeypatch.setattr(
        enterprise.platform_service, "get_public_base_url", AsyncMock(return_value="https://app.invalid")
    )
    connections = []

    async def create(**kwargs):
        connections.append(session.get_connection())
        if len(connections) == 2:
            raise RuntimeError("second invitation failed")
        return SimpleNamespace()

    monkeypatch.setattr(enterprise.invitation_code_dao, "create", create)
    with pytest.raises(RuntimeError, match="second invitation"):
        await enterprise.invite_users(
            Request({"type": "http"}),
            enterprise.UserInviteRequest(emails=["one@example.com", "two@example.com"]),
            background,
            user,
        )
    assert connections[0] is not None
    assert connections[0] is connections[1]
    assert background.tasks == []
    assert owned_pool.raw.rollbacks == 1


async def test_isolated_http_probe_uses_no_request_wide_checkout(monkeypatch, owned_pool):
    model = _model()
    user = _actor(model)
    app = FastAPI()
    app.include_router(enterprise.router)
    app.dependency_overrides[enterprise.get_current_admin] = lambda: user
    monkeypatch.setattr(enterprise.llm_model_dao, "get", AsyncMock(return_value=model))
    monkeypatch.setattr(enterprise.user_dao, "get_with_identity", AsyncMock(return_value=user))

    async def complete(**kwargs):
        assert session.get_connection() is None
        assert owned_pool.active == 0
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(enterprise, "create_llm_client", lambda **kwargs: SimpleNamespace(complete=complete))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/enterprise/llm-test",
            json={
                "provider": model.provider,
                "model": model.model,
                "model_id": str(model.id),
            },
        )
        assert response.status_code == 200
        assert response.json()["reply"] == "ok"
        invalid = await client.post("/enterprise/llm-test", json={})
        assert invalid.status_code == 422
