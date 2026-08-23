"""Company ChatGPT Plus / Pro / Team handoff: start, poll, persist, slots, refresh."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api import enterprise as enterprise_api
from app.records.llm import LLMModelRecord
from app.services import chatgpt_oauth, chatgpt_subscription as handoff, enterprise_llm as pool
from app.services.chatgpt_oauth import (
    OPENAI_DEVICE_POLL_URL,
    OPENAI_DEVICE_REDIRECT_URI,
    OPENAI_TOKEN_URL,
    OPENAI_USERCODE_URL,
    OPENAI_USERINFO_URL,
    interpret_device_poll,
    set_chatgpt_oauth_transport,
)

_NOW = datetime.now(UTC)
_TENANT = uuid.uuid4()
_OTHER = uuid.uuid4()


def _jwt(payload: dict[str, Any]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


_ACCESS = _jwt({"chatgpt_account_id": "acct-live", "sub": "user-1"})
_REFRESHED = _jwt({"chatgpt_account_id": "acct-live", "sub": "user-1", "iat": 2})


def _user(*, role="org_admin", tenant_id: uuid.UUID | None = _TENANT):
    return SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=tenant_id, identity=None)


def _model(**kwargs) -> LLMModelRecord:
    defaults = {
        "id": uuid.uuid4(),
        "provider": "openai",
        "model": "gpt-5.4",
        "api_key_encrypted": "enc-secret-key-9999",
        "label": "ChatGPT",
        "tenant_id": _TENANT,
        "base_url": "https://chatgpt.com/backend-api/codex",
        "max_tokens_per_day": None,
        "enabled": True,
        "supports_vision": True,
        "temperature": None,
        "request_timeout": None,
        "max_output_tokens": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "auth_kind": "chatgpt_subscription",
        "refresh_token_encrypted": "enc-refresh",
        "token_expires_at": _NOW + timedelta(hours=1),
    }
    defaults.update(kwargs)
    return LLMModelRecord(**defaults)


class FakeChatGPTTransport:
    """In-process OpenAI stand-in. Records JSON and form posts."""

    def __init__(self, *, pending_polls: int = 0) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.pending_polls = pending_polls
        self.usercode_body = {
            "device_auth_id": "dev-secret-must-not-leak",
            "user_code": "ABCD-EFGH",
            "interval": "10",
        }
        self.poll_status = 200
        self.poll_body: dict[str, Any] = {
            "authorization_code": "auth-code-LIVE",
            "code_challenge": "challenge-LIVE",
            "code_verifier": "verifier-LIVE",
        }
        self.token_status = 200
        self.token_body: dict[str, Any] = {
            "access_token": _ACCESS,
            "refresh_token": "oa-refresh-LIVE",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid profile email offline_access",
            "id_token": _ACCESS,
        }
        self.userinfo_status = 200
        self.userinfo_body: dict[str, Any] = {"email": "admin@example.com"}

    async def post_json(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, "json", dict(data)))
        if url == OPENAI_USERCODE_URL:
            return 200, self.usercode_body
        if url == OPENAI_DEVICE_POLL_URL:
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return 403, {}
            return self.poll_status, self.poll_body
        return 404, {"error": "not_found"}

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, "form", dict(data)))
        if url == OPENAI_TOKEN_URL:
            if data.get("grant_type") == "refresh_token":
                return 200, {**self.token_body, "access_token": _REFRESHED}
            return self.token_status, self.token_body
        return 404, {"error": "not_found"}

    async def get_json(self, url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        self.calls.append((url, "get", dict(headers)))
        if url == OPENAI_USERINFO_URL:
            return self.userinfo_status, self.userinfo_body
        return 404, {"error": "not_found"}


@pytest.fixture(autouse=True)
def _reset_handoff():
    handoff.reset_chatgpt_sessions()
    set_chatgpt_oauth_transport(None)
    yield
    handoff.reset_chatgpt_sessions()
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
async def test_start_returns_verification_url_and_user_code_without_tokens():
    transport = FakeChatGPTTransport()
    set_chatgpt_oauth_transport(transport)

    out = await enterprise_api.start_chatgpt_subscription(current_user=_user())

    assert out.verification_url == "https://auth.openai.com/codex/device"
    assert out.user_code == "ABCD-EFGH"
    assert out.session_id
    assert out.interval == 10
    assert out.expires_in >= 870
    _assert_no_secrets(out.model_dump())
    assert transport.calls, "start must invoke the shipped Codex usercode client"
    assert transport.calls[0][0] == OPENAI_USERCODE_URL
    assert transport.calls[0][2]["client_id"] == chatgpt_oauth.OPENAI_OAUTH_CLIENT_ID


@pytest.mark.asyncio
async def test_start_rejects_member_and_foreign_tenant():
    set_chatgpt_oauth_transport(FakeChatGPTTransport())
    with pytest.raises(HTTPException) as member:
        await enterprise_api.start_chatgpt_subscription(current_user=_user(role="member"))
    assert member.value.status_code == 403

    with pytest.raises(HTTPException) as foreign:
        await enterprise_api.start_chatgpt_subscription(
            tenant_id=str(_OTHER), current_user=_user(role="org_admin")
        )
    assert foreign.value.status_code == 403


@pytest.mark.asyncio
async def test_status_persists_encrypted_pool_row_and_hides_tokens(monkeypatch):
    transport = FakeChatGPTTransport()
    set_chatgpt_oauth_transport(transport)
    created = _model(api_key_encrypted=f"enc:{_ACCESS}", refresh_token_encrypted="enc:oa-refresh-LIVE")
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(handoff.llm_model_dao, "get_subscription_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(handoff.llm_model_dao, "create", create)
    monkeypatch.setattr(handoff, "encrypt_data", lambda value, _key: f"enc:{value}")
    monkeypatch.setattr(handoff, "activate_pool_model_for_tenant", AsyncMock())

    start = await enterprise_api.start_chatgpt_subscription(current_user=_user())
    status = await enterprise_api.get_chatgpt_subscription_status(
        session_id=start.session_id, current_user=_user()
    )

    assert status.status == "authorized"
    assert status.model_id == created.id
    _assert_no_secrets(status.model_dump())
    poll_calls = [call for call in transport.calls if call[0] == OPENAI_DEVICE_POLL_URL]
    exchange_calls = [
        call
        for call in transport.calls
        if call[0] == OPENAI_TOKEN_URL and call[2].get("grant_type") == "authorization_code"
    ]
    assert poll_calls, "status must poll the shipped Codex deviceauth endpoint"
    assert exchange_calls, "status must exchange the authorization code"
    create.assert_awaited_once()
    obj_in = create.await_args.kwargs["obj_in"]
    assert obj_in["api_key_encrypted"] == f"enc:{_ACCESS}"
    assert obj_in["refresh_token_encrypted"] == "enc:oa-refresh-LIVE"
    assert obj_in["api_key_encrypted"] != _ACCESS
    assert obj_in["refresh_token_encrypted"] != "oa-refresh-LIVE"
    assert obj_in["auth_kind"] == "chatgpt_subscription"
    assert obj_in["tenant_id"] == _TENANT
    assert obj_in["provider"] == "openai"
    assert obj_in["model"] == "gpt-5.4"
    assert obj_in["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert obj_in["oauth_account_id"] == "acct-live"
    poll_json = poll_calls[0][2]
    assert poll_json == {"device_auth_id": "dev-secret-must-not-leak", "user_code": "ABCD-EFGH"}
    assert exchange_calls[0][2] == {
        "grant_type": "authorization_code",
        "code": "auth-code-LIVE",
        "redirect_uri": OPENAI_DEVICE_REDIRECT_URI,
        "client_id": chatgpt_oauth.OPENAI_OAUTH_CLIENT_ID,
        "code_verifier": "verifier-LIVE",
    }

    with patch.object(pool, "get_model_api_key", return_value=_ACCESS):
        admin = pool.serialize_llm_model(created, is_admin=True, default_model_id=None)
    assert admin.api_key_masked == ""
    assert admin.auth_kind == "chatgpt_subscription"
    member = pool.serialize_llm_model(created, is_admin=False, default_model_id=None)
    assert member.api_key_masked == ""
    assert member.base_url is None


@pytest.mark.asyncio
async def test_status_rejects_member_and_other_tenant_org_admin():
    set_chatgpt_oauth_transport(FakeChatGPTTransport())
    start = await enterprise_api.start_chatgpt_subscription(current_user=_user())

    with pytest.raises(HTTPException) as member:
        await enterprise_api.get_chatgpt_subscription_status(
            session_id=start.session_id, current_user=_user(role="member")
        )
    assert member.value.status_code == 403

    with pytest.raises(HTTPException) as foreign:
        await enterprise_api.get_chatgpt_subscription_status(
            session_id=start.session_id,
            current_user=_user(role="org_admin", tenant_id=_OTHER),
        )
    assert foreign.value.status_code == 403


@pytest.mark.asyncio
async def test_status_stays_pending_until_openai_authorizes():
    transport = FakeChatGPTTransport(pending_polls=1)
    set_chatgpt_oauth_transport(transport)
    start = await enterprise_api.start_chatgpt_subscription(current_user=_user())
    pending = await enterprise_api.get_chatgpt_subscription_status(
        session_id=start.session_id, current_user=_user()
    )
    assert pending.status == "pending"
    assert pending.user_code == "ABCD-EFGH"
    _assert_no_secrets(pending.model_dump())


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
    obj_in = persist.await_args.kwargs["obj_in"]
    assert obj_in["api_key_encrypted"] == f"enc:{_REFRESHED}"
    assert "model" not in obj_in
    assert "base_url" not in obj_in
    assert "provider" not in obj_in
    refresh_calls = [
        call
        for call in transport.calls
        if call[0] == OPENAI_TOKEN_URL and call[2].get("grant_type") == "refresh_token"
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
async def test_probe_uses_codex_responses_client(monkeypatch):
    existing = _model(api_key_encrypted="enc-sub-secret", oauth_account_id="acct-live")
    monkeypatch.setattr(enterprise_api.llm_model_dao, "get", AsyncMock(return_value=existing))
    monkeypatch.setattr(enterprise_api, "get_model_api_key", lambda _model: _ACCESS)
    monkeypatch.setattr(
        "app.services.chatgpt_subscription.ensure_fresh_access_token",
        AsyncMock(return_value=existing),
    )

    captured: dict[str, Any] = {}

    class FakeClient:
        async def complete(self, messages, max_tokens=16):
            _ = messages, max_tokens
            return SimpleNamespace(content="ok")

        async def close(self):
            return None

    def fake_from_model(model, *, timeout=None):
        captured["auth_kind"] = getattr(model, "auth_kind", None)
        captured["timeout"] = timeout
        return FakeClient()

    monkeypatch.setattr(enterprise_api, "create_llm_client_from_model", fake_from_model)
    monkeypatch.setattr(enterprise_api, "create_llm_client", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not use chat-completions")))

    result = await enterprise_api.probe_llm_model(
        enterprise_api.LLMTestRequest(
            provider="openai",
            model="gpt-5.4",
            model_id=str(existing.id),
        ),
        current_user=_user(),
    )
    assert result["success"] is True
    assert result["reply"] == "ok"
    assert captured["auth_kind"] == "chatgpt_subscription"


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
    assert tenant_update.await_args.kwargs["obj_in"]["default_model_id"] == created.id
    assert migrate.await_args.kwargs["old_model_id"] == dummy_id
    assert migrate.await_args.kwargs["new_model_id"] == created.id


@pytest.mark.asyncio
async def test_ensure_agent_inherits_chatgpt_subscription_when_primary_missing(monkeypatch):
    chatgpt = _model()
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
    agent = SimpleNamespace(
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
    assert update.await_args.kwargs["obj_in"]["primary_model_id"] == chatgpt.id


def test_interpret_device_poll_uses_http_status_not_rfc_error_string():
    assert interpret_device_poll(403, {}).status == "pending"
    assert interpret_device_poll(404, {}).status == "pending"
    assert interpret_device_poll(400, {"error": "authorization_pending"}).status == "error"


@pytest.mark.asyncio
async def test_activate_chatgpt_leaves_grok_primary(monkeypatch):
    created = _model()
    grok_id = uuid.uuid4()
    grok = _model(
        id=grok_id,
        provider="grok",
        model="grok-4.6",
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
    assert tenant_update.await_args.kwargs["obj_in"]["default_fallback_model_id"] == created.id
    assert tenant.default_model_id == grok_id


def test_create_llm_client_from_model_uses_codex_responses():
    from app.services.llm.providers.openai_responses import OpenAIResponsesClient
    from app.services.llm.utils import create_llm_client_from_model

    row = _model(api_key_encrypted=_ACCESS, oauth_account_id="acct-stored")
    client = create_llm_client_from_model(row, timeout=9)
    assert type(client) is OpenAIResponsesClient
    assert client.stateless is True
    assert client.base_url == "https://chatgpt.com/backend-api/codex"
    headers = client._get_headers()
    assert headers["chatgpt-account-id"] == "acct-stored"
    assert headers["originator"] == "codex_cli_rs"
    payload = client._build_payload([], None, None, 16)
    assert payload["store"] is False
    assert "max_output_tokens" not in payload
