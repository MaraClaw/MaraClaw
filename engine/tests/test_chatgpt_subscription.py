"""ChatGPT subscription refresh and pool activation regressions."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from tests.chatgpt_subscription_fakes import (
    _ACCESS,
    _NOW,
    _OTHER,
    _REFRESHED,
    _TENANT,
    FakeChatGPTTransport,
    _model,
    _user,
)

from app.api import enterprise as enterprise_api
from app.services import chatgpt_subscription as handoff, enterprise_llm as pool
from app.services.chatgpt_oauth import (
    OPENAI_TOKEN_URL,
    set_chatgpt_oauth_transport,
)

__all__ = ["_ACCESS", "_REFRESHED", "FakeChatGPTTransport", "_model", "_user"]


@pytest.fixture(autouse=True)
def _reset_handoff():
    set_chatgpt_oauth_transport(None)
    yield
    set_chatgpt_oauth_transport(None)


def _assert_no_secrets(payload: dict[str, Any]) -> None:
    leaked = {
        "access_token",
        "refresh_token",
        "id_token",
        "device_code",
        "device_auth_id",
        "authorization_code",
        "code_verifier",
        "code_challenge",
        "cookie",
        "cookies",
        "password",
        "api_key",
        "token",
    }.intersection(payload)
    assert not leaked, f"secret fields leaked: {sorted(leaked)}"
    blob = str(payload)
    assert "dev-secret" not in blob
    assert "oa-refresh-" not in blob
    assert "auth-code-" not in blob
    assert "verifier-LIVE" not in blob


@pytest.mark.asyncio
async def test_refresh_updates_encrypted_access_without_returning_tokens(monkeypatch):
    transport = FakeChatGPTTransport()
    set_chatgpt_oauth_transport(transport)
    existing = _model(
        token_expires_at=_NOW - timedelta(minutes=1),
        refresh_token_encrypted="enc-refresh-old",
    )
    updated = _model(api_key_encrypted=f"enc:{_REFRESHED}")
    monkeypatch.setattr(handoff.llm_model_dao, "get", AsyncMock(return_value=existing))
    monkeypatch.setattr(handoff, "decrypt_data", lambda value, _key: "oa-refresh-LIVE")
    monkeypatch.setattr(handoff, "encrypt_data", lambda value, _key: f"enc:{value}")
    persist = AsyncMock(return_value=updated)
    monkeypatch.setattr(handoff.llm_model_dao, "update", persist)
    monkeypatch.setattr(handoff, "activate_pool_model_for_tenant", AsyncMock())

    out = await enterprise_api.refresh_grok_subscription(existing.id, current_user=_user())
    assert out.ok is True
    assert out.model_id == updated.id
    _assert_no_secrets(out.model_dump())
    persist.assert_awaited_once()
    assert persist.await_args is not None
    obj_in = persist.await_args.kwargs["obj_in"]
    assert obj_in["api_key_encrypted"] == f"enc:{_REFRESHED}"
    assert "model" not in obj_in
    assert "base_url" not in obj_in
    assert "provider" not in obj_in
    refresh_calls = [
        call for call in transport.calls if call[0] == OPENAI_TOKEN_URL and call[2].get("grant_type") == "refresh_token"
    ]
    assert refresh_calls, "refresh must invoke the shipped token refresh"


@pytest.mark.asyncio
async def test_refresh_rejects_member_and_foreign_tenant(monkeypatch):
    existing = _model(tenant_id=_OTHER)
    monkeypatch.setattr(handoff.llm_model_dao, "get", AsyncMock(return_value=existing))
    with pytest.raises(HTTPException) as member:
        await enterprise_api.refresh_grok_subscription(existing.id, current_user=_user(role="member"))
    assert member.value.status_code == 403
    with pytest.raises(HTTPException) as foreign:
        await enterprise_api.refresh_grok_subscription(existing.id, current_user=_user(role="org_admin"))
    assert foreign.value.status_code == 403


@pytest.mark.asyncio
async def test_activate_subscription_replaces_openai_api_key_default(monkeypatch):
    created = _model()
    dummy_id = uuid.uuid4()
    dummy = _model(id=dummy_id, auth_kind="api_key", label="GPT-5.4", base_url="https://api.openai.com/v1")
    tenant = SimpleNamespace(id=_TENANT, default_model_id=dummy_id, default_fallback_model_id=None)
    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.llm_model_dao, "get", AsyncMock(return_value=dummy))
    monkeypatch.setattr(pool.llm_model_dao, "list_for_tenant", AsyncMock(return_value=[dummy, created]))
    tenant_update = AsyncMock(return_value=tenant)
    migrate = AsyncMock(return_value=2)
    assign = AsyncMock(return_value=0)
    monkeypatch.setattr(pool.tenant_dao, "update", tenant_update)
    monkeypatch.setattr(pool.agent_dao, "migrate_primary_model", migrate)
    monkeypatch.setattr(pool.agent_dao, "assign_primary_where_null", assign)
    await pool.activate_pool_model_for_tenant(created)
    assert tenant_update.await_args is not None
    assert migrate.await_args is not None
    assert tenant_update.await_args.kwargs["obj_in"]["default_model_id"] == created.id
    assert migrate.await_args.kwargs["old_model_id"] == dummy_id
    assert migrate.await_args.kwargs["new_model_id"] == created.id


@pytest.mark.asyncio
async def test_ensure_agent_inherits_chatgpt_subscription_when_primary_missing(monkeypatch):
    chatgpt = _model()
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        primary_model_id=None,
        secondary_model_id=None,
        fallback_model_id=None,
    )
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=None,
        default_secondary_model_id=None,
        default_fallback_model_id=None,
    )

    async def load_subscription(_tenant_id, *, auth_kind="grok_subscription"):
        return chatgpt if auth_kind == "chatgpt_subscription" else None

    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.llm_model_dao, "get_many", AsyncMock(return_value=[]))
    monkeypatch.setattr(pool.llm_model_dao, "get_subscription_for_tenant", AsyncMock(side_effect=load_subscription))
    monkeypatch.setattr(pool.llm_model_dao, "list_for_tenant", AsyncMock(return_value=[chatgpt]))
    monkeypatch.setattr(pool.tenant_dao, "update", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.agent_dao, "assign_primary_where_null", AsyncMock(return_value=1))
    saved = SimpleNamespace(**{**agent.__dict__, "primary_model_id": chatgpt.id})
    monkeypatch.setattr(pool.agent_dao, "update", AsyncMock(return_value=saved))
    out = await pool.ensure_agent_company_models(agent)
    assert out.primary_model_id == chatgpt.id


@pytest.mark.asyncio
async def test_ensure_agent_prefers_subscription_over_openai_api_key(monkeypatch):
    chatgpt = _model()
    dummy = _model(
        id=uuid.uuid4(),
        auth_kind="api_key",
        label="GPT-5.4",
        api_key_encrypted="enc-short",
        refresh_token_encrypted=None,
        base_url="https://api.openai.com/v1",
    )
    agent: Any = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        primary_model_id=dummy.id,
        secondary_model_id=None,
        fallback_model_id=None,
    )
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=dummy.id,
        default_secondary_model_id=None,
        default_fallback_model_id=None,
    )

    async def load_subscription(_tenant_id, *, auth_kind="grok_subscription"):
        return chatgpt if auth_kind == "chatgpt_subscription" else None

    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.llm_model_dao, "get_many", AsyncMock(return_value=[dummy]))
    monkeypatch.setattr(pool.llm_model_dao, "get_subscription_for_tenant", AsyncMock(side_effect=load_subscription))
    monkeypatch.setattr(pool.tenant_dao, "update", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.agent_dao, "assign_primary_where_null", AsyncMock(return_value=0))
    saved = SimpleNamespace(**{**agent.__dict__, "primary_model_id": chatgpt.id})
    update = AsyncMock(return_value=saved)
    monkeypatch.setattr(pool.agent_dao, "update", update)
    out = await pool.ensure_agent_company_models(agent)
    assert out.primary_model_id == chatgpt.id
    assert update.await_args is not None
    assert update.await_args.kwargs["obj_in"]["primary_model_id"] == chatgpt.id


@pytest.mark.asyncio
async def test_activate_chatgpt_leaves_grok_primary(monkeypatch):
    created = _model()
    grok_id = uuid.uuid4()
    grok = _model(
        id=grok_id,
        provider="grok",
        model="grok-4.8",
        auth_kind="grok_subscription",
        label="Grok SuperGrok",
        base_url="https://api.x.ai/v1",
    )
    tenant = SimpleNamespace(id=_TENANT, default_model_id=grok_id, default_fallback_model_id=None)
    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.llm_model_dao, "get", AsyncMock(return_value=grok))
    tenant_update = AsyncMock(return_value=tenant)
    monkeypatch.setattr(pool.tenant_dao, "update", tenant_update)
    monkeypatch.setattr(pool.agent_dao, "assign_primary_where_null", AsyncMock(return_value=0))
    await pool.activate_pool_model_for_tenant(created)
    assert tenant_update.await_args is not None
    assert tenant_update.await_args.kwargs["obj_in"]["default_fallback_model_id"] == created.id
    assert tenant.default_model_id == grok_id


@pytest.mark.asyncio
async def test_update_ignores_api_key_on_chatgpt_subscription(monkeypatch):
    existing = _model()
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=existing))
    captured: dict[str, Any] = {}

    async def persist(*, db_obj, obj_in):
        captured.update(obj_in)
        return existing

    monkeypatch.setattr(enterprise_api.llm_model_dao, "update", persist)
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=None))
    from app.schemas.schemas import LLMModelUpdate

    await enterprise_api.update_llm_model(
        existing.id,
        LLMModelUpdate(
            label="ChatGPT Plus",
            api_key="sk-should-not-write",
            base_url="https://evil.example",
            temperature=None,
            reasoning_effort=None,
        ),
        current_user=_user(),
    )
    assert "api_key_encrypted" not in captured
    assert "base_url" not in captured
    assert captured["label"] == "ChatGPT Plus"
