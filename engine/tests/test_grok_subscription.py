"""Company Grok SuperGrok / X Premium handoff: start, poll, persist, slots, refresh."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import enterprise as enterprise_api
from app.records.llm import LLMModelRecord
from app.services import enterprise_llm as pool, grok_oauth, grok_subscription as handoff
from app.services.grok_oauth import (
    XAI_DEVICE_CODE_URL,
    XAI_TOKEN_URL,
    set_grok_oauth_transport,
)

_NOW = datetime.now(UTC)
_TENANT = uuid.uuid4()
_OTHER = uuid.uuid4()


def _user(*, role="org_admin", tenant_id: uuid.UUID | None = _TENANT):
    return SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=tenant_id, identity=None)


def _model(**kwargs) -> LLMModelRecord:
    defaults = {
        "id": uuid.uuid4(),
        "provider": "grok",
        "model": "grok-4.6",
        "api_key_encrypted": "enc-secret-key-9999",
        "label": "Grok SuperGrok",
        "tenant_id": _TENANT,
        "base_url": "https://api.x.ai/v1",
        "max_tokens_per_day": None,
        "enabled": True,
        "supports_vision": True,
        "temperature": None,
        "request_timeout": None,
        "max_output_tokens": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "auth_kind": "grok_subscription",
        "refresh_token_encrypted": "enc-refresh",
        "token_expires_at": _NOW + timedelta(hours=1),
    }
    defaults.update(kwargs)
    return LLMModelRecord(**defaults)


class FakeXaiTransport:
    """In-process xAI stand-in. Records every form POST the shipped client makes."""

    def __init__(self, *, pending_polls: int = 0) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.pending_polls = pending_polls
        self.device_body = {
            "device_code": "dev-secret-must-not-leak",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH",
            "expires_in": 1800,
            "interval": 5,
        }
        self.token_status = 200
        self.token_body: dict[str, Any] = {
            "access_token": "xai-access-LIVE",
            "refresh_token": "xai-refresh-LIVE",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "offline_access api:access",
        }

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, dict(data)))
        if url == XAI_DEVICE_CODE_URL:
            return 200, self.device_body
        if url == XAI_TOKEN_URL:
            if data.get("grant_type") == "refresh_token":
                return 200, {**self.token_body, "access_token": "xai-access-REFRESHED"}
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return 400, {"error": "authorization_pending"}
            return self.token_status, self.token_body
        return 404, {"error": "not_found"}


@pytest.fixture(autouse=True)
def _reset_handoff():
    handoff.reset_grok_sessions()
    set_grok_oauth_transport(None)
    yield
    handoff.reset_grok_sessions()
    set_grok_oauth_transport(None)


def _assert_no_secrets(payload: dict[str, Any]) -> None:
    leaked = {
        "access_token",
        "refresh_token",
        "id_token",
        "device_code",
        "cookie",
        "cookies",
        "password",
        "api_key",
        "token",
    }.intersection(payload)
    assert not leaked, f"secret fields leaked: {sorted(leaked)}"
    blob = str(payload)
    assert "xai-access-" not in blob
    assert "xai-refresh-" not in blob
    assert "dev-secret" not in blob


@pytest.mark.asyncio
async def test_start_returns_verification_url_and_user_code_without_tokens():
    transport = FakeXaiTransport()
    set_grok_oauth_transport(transport)

    out = await enterprise_api.start_grok_subscription(current_user=_user())

    assert out.verification_url == "https://accounts.x.ai/oauth2/device?user_code=ABCD-EFGH"
    assert out.user_code == "ABCD-EFGH"
    assert out.session_id
    assert out.interval == 5
    _assert_no_secrets(out.model_dump())
    assert transport.calls, "start must invoke the shipped xAI device-code client"
    assert transport.calls[0][0] == XAI_DEVICE_CODE_URL
    assert transport.calls[0][1]["client_id"] == grok_oauth.XAI_OAUTH_CLIENT_ID


@pytest.mark.asyncio
async def test_start_rejects_member_and_foreign_tenant():
    set_grok_oauth_transport(FakeXaiTransport())
    with pytest.raises(HTTPException) as member:
        await enterprise_api.start_grok_subscription(current_user=_user(role="member"))
    assert member.value.status_code == 403

    with pytest.raises(HTTPException) as foreign:
        await enterprise_api.start_grok_subscription(
            tenant_id=str(_OTHER), current_user=_user(role="org_admin")
        )
    assert foreign.value.status_code == 403


@pytest.mark.asyncio
async def test_status_persists_encrypted_pool_row_and_hides_tokens(monkeypatch):
    transport = FakeXaiTransport()
    set_grok_oauth_transport(transport)
    created = _model(api_key_encrypted="enc:xai-access-LIVE", refresh_token_encrypted="enc:xai-refresh-LIVE")
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(handoff.llm_model_dao, "get_subscription_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(handoff.llm_model_dao, "create", create)
    monkeypatch.setattr(handoff, "encrypt_data", lambda value, _key: f"enc:{value}")
    monkeypatch.setattr(handoff, "activate_pool_model_for_tenant", AsyncMock())

    start = await enterprise_api.start_grok_subscription(current_user=_user())
    status = await enterprise_api.get_grok_subscription_status(
        session_id=start.session_id, current_user=_user()
    )

    assert status.status == "authorized"
    assert status.model_id == created.id
    _assert_no_secrets(status.model_dump())
    token_calls = [call for call in transport.calls if call[0] == XAI_TOKEN_URL]
    assert token_calls, "status must poll the shipped xAI token endpoint"
    create.assert_awaited_once()
    obj_in = create.await_args.kwargs["obj_in"]
    assert obj_in["api_key_encrypted"] == "enc:xai-access-LIVE"
    assert obj_in["refresh_token_encrypted"] == "enc:xai-refresh-LIVE"
    assert obj_in["api_key_encrypted"] != "xai-access-LIVE"
    assert obj_in["refresh_token_encrypted"] != "xai-refresh-LIVE"
    assert obj_in["auth_kind"] == "grok_subscription"
    assert obj_in["tenant_id"] == _TENANT
    assert obj_in["provider"] == "grok"

    with patch.object(pool, "get_model_api_key", return_value="xai-access-LIVE"):
        admin = pool.serialize_llm_model(created, is_admin=True, default_model_id=None)
    assert admin.api_key_masked == "****LIVE"
    assert admin.auth_kind == "grok_subscription"
    member = pool.serialize_llm_model(created, is_admin=False, default_model_id=None)
    assert member.api_key_masked == ""
    assert member.base_url is None


@pytest.mark.asyncio
async def test_status_rejects_member_and_other_tenant_org_admin():
    set_grok_oauth_transport(FakeXaiTransport())
    start = await enterprise_api.start_grok_subscription(current_user=_user())

    with pytest.raises(HTTPException) as member:
        await enterprise_api.get_grok_subscription_status(
            session_id=start.session_id, current_user=_user(role="member")
        )
    assert member.value.status_code == 403

    with pytest.raises(HTTPException) as foreign:
        await enterprise_api.get_grok_subscription_status(
            session_id=start.session_id,
            current_user=_user(role="org_admin", tenant_id=_OTHER),
        )
    assert foreign.value.status_code == 403


@pytest.mark.asyncio
async def test_subscription_model_can_fill_primary_then_distinct_secondary_and_fallback(monkeypatch):
    primary = _model(label="Grok SuperGrok")
    secondary = _model(provider="anthropic", model="claude-sonnet-4-5", label="Claude", auth_kind="api_key")
    fallback = _model(provider="openai", model="gpt-5.6", label="GPT", auth_kind="api_key")
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=None,
        default_fallback_model_id=None,
        default_secondary_model_id=None,
    )

    async def load_model(model_id):
        for row in (primary, secondary, fallback):
            if row.id == model_id:
                return row
        return None

    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(side_effect=load_model))
    monkeypatch.setattr(enterprise_api.tenant_dao, "get", AsyncMock(return_value=tenant))
    updates: list[dict[str, Any]] = []

    async def tenant_update(*, db_obj, obj_in):
        updates.append(dict(obj_in))
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    monkeypatch.setattr(enterprise_api.tenant_dao, "update", tenant_update)
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_primary_model", AsyncMock(return_value=0))
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_secondary_model", AsyncMock(return_value=0))
    monkeypatch.setattr(enterprise_api.agent_dao, "migrate_fallback_model", AsyncMock(return_value=0))
    monkeypatch.setattr(enterprise_api.agent_dao, "clear_other_slots_matching", AsyncMock())

    await enterprise_api.set_default_llm_model(primary.id, current_user=_user())
    await enterprise_api.set_secondary_llm_model(secondary.id, current_user=_user())
    await enterprise_api.set_fallback_llm_model(fallback.id, current_user=_user())
    assert tenant.default_model_id == primary.id
    assert tenant.default_secondary_model_id == secondary.id
    assert tenant.default_fallback_model_id == fallback.id

    with pytest.raises(HTTPException) as same:
        await enterprise_api.set_secondary_llm_model(primary.id, current_user=_user())
    assert same.value.status_code == 400


@pytest.mark.asyncio
async def test_refresh_updates_encrypted_access_without_returning_tokens(monkeypatch):
    transport = FakeXaiTransport()
    set_grok_oauth_transport(transport)
    existing = _model(
        token_expires_at=_NOW - timedelta(minutes=1),
        refresh_token_encrypted="enc-refresh-old",
    )
    updated = _model(api_key_encrypted="enc:xai-access-REFRESHED")
    monkeypatch.setattr(handoff.llm_model_dao, "get", AsyncMock(return_value=existing))
    monkeypatch.setattr(handoff, "decrypt_data", lambda value, _key: "xai-refresh-LIVE")
    monkeypatch.setattr(handoff, "encrypt_data", lambda value, _key: f"enc:{value}")
    persist = AsyncMock(return_value=updated)
    monkeypatch.setattr(handoff.llm_model_dao, "update", persist)
    monkeypatch.setattr(handoff, "activate_pool_model_for_tenant", AsyncMock())

    out = await enterprise_api.refresh_grok_subscription(existing.id, current_user=_user())
    assert out.ok is True
    assert out.model_id == updated.id
    _assert_no_secrets(out.model_dump())
    persist.assert_awaited_once()
    obj_in = persist.await_args.kwargs["obj_in"]
    assert obj_in["api_key_encrypted"] == "enc:xai-access-REFRESHED"
    refresh_calls = [
        call
        for call in transport.calls
        if call[0] == XAI_TOKEN_URL and call[1].get("grant_type") == "refresh_token"
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
async def test_probe_uses_stored_subscription_secret(monkeypatch):
    existing = _model(api_key_encrypted="enc-sub-secret")
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=existing))
    monkeypatch.setattr(enterprise_api, "get_model_api_key", lambda _model: "xai-access-LIVE")
    monkeypatch.setattr(
        "app.services.grok_subscription.ensure_fresh_access_token",
        AsyncMock(return_value=existing),
    )

    class FakeClient:
        def __init__(self) -> None:
            self.seen_key: str | None = None

        async def complete(self, messages, max_tokens=16):
            _ = messages, max_tokens
            return SimpleNamespace(content="ok")

    captured: dict[str, Any] = {}

    def fake_create(*, provider, model, api_key, base_url=None):
        captured["provider"] = provider
        captured["model"] = model
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        return FakeClient()

    monkeypatch.setattr(enterprise_api, "create_llm_client", fake_create)
    result = await enterprise_api.probe_llm_model(
        enterprise_api.LLMTestRequest(
            provider="grok",
            model="grok-4.6",
            model_id=str(existing.id),
        ),
        current_user=_user(),
    )
    assert result["success"] is True
    assert result["reply"] == "ok"
    assert captured["api_key"] == "xai-access-LIVE"
    assert captured["provider"] == "grok"


@pytest.mark.asyncio
async def test_persist_assigns_company_primary_and_bare_agents(monkeypatch):
    created = _model()
    tenant = SimpleNamespace(id=_TENANT, default_model_id=None, default_fallback_model_id=None)
    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    tenant_update = AsyncMock(return_value=tenant)
    assign = AsyncMock(return_value=2)
    monkeypatch.setattr(pool.tenant_dao, "update", tenant_update)
    monkeypatch.setattr(pool.agent_dao, "assign_primary_where_null", assign)
    await pool.activate_pool_model_for_tenant(created)
    assert tenant_update.await_args.kwargs["obj_in"]["default_model_id"] == created.id
    assign.assert_awaited_once()
    assert assign.await_args.kwargs["model_id"] == created.id


@pytest.mark.asyncio
async def test_ensure_agent_inherits_grok_subscription_when_primary_missing(monkeypatch):
    grok = _model()
    agent = SimpleNamespace(
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
    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.llm_model_dao, "get_many", AsyncMock(return_value=[]))
    monkeypatch.setattr(pool.llm_model_dao, "list_for_tenant", AsyncMock(return_value=[grok]))
    monkeypatch.setattr(pool.tenant_dao, "update", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.agent_dao, "assign_primary_where_null", AsyncMock(return_value=1))
    saved = SimpleNamespace(**{**agent.__dict__, "primary_model_id": grok.id})
    monkeypatch.setattr(pool.agent_dao, "update", AsyncMock(return_value=saved))
    out = await pool.ensure_agent_company_models(agent)
    assert out.primary_model_id == grok.id


@pytest.mark.asyncio
async def test_ensure_agent_replaces_foreign_primary(monkeypatch):
    grok = _model()
    foreign_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        primary_model_id=foreign_id,
        secondary_model_id=None,
        fallback_model_id=None,
    )
    tenant = SimpleNamespace(
        id=_TENANT,
        default_model_id=grok.id,
        default_secondary_model_id=None,
        default_fallback_model_id=None,
    )
    monkeypatch.setattr(pool.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(pool.llm_model_dao, "get_many", AsyncMock(return_value=[grok]))
    saved = SimpleNamespace(**{**agent.__dict__, "primary_model_id": grok.id})
    update = AsyncMock(return_value=saved)
    monkeypatch.setattr(pool.agent_dao, "update", update)
    out = await pool.ensure_agent_company_models(agent)
    assert out.primary_model_id == grok.id
    assert update.await_args.kwargs["obj_in"]["primary_model_id"] == grok.id


@pytest.mark.asyncio
async def test_status_stays_pending_until_xai_authorizes():
    transport = FakeXaiTransport(pending_polls=1)
    set_grok_oauth_transport(transport)
    start = await enterprise_api.start_grok_subscription(current_user=_user())
    pending = await enterprise_api.get_grok_subscription_status(
        session_id=start.session_id, current_user=_user()
    )
    assert pending.status == "pending"
    assert pending.user_code == "ABCD-EFGH"
    _assert_no_secrets(pending.model_dump())
