from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from tests.transaction_pool_fakes import CountingPool

from app.api import auth, enterprise, google_chat, google_workspace, slack, sso, teams
from app.config import Settings
from app.core.domain.users import UserRole
from app.core.security import get_current_admin, get_current_user
from app.dao.sso_scan_session_dao import sso_scan_session_dao
from app.db import session
from app.main import app
from app.records.identity import IdentityProviderRecord
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.services import google_workspace_oauth, platform_service as platform_module, system_email_service


@pytest.fixture
def url_actor(monkeypatch):
    actor = UserRecord(id=uuid4(), tenant_id=uuid4(), identity_id=uuid4(), role=UserRole.PLATFORM_ADMIN)
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: actor)
    monkeypatch.setitem(app.dependency_overrides, get_current_admin, lambda: actor)
    return actor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        "channel",
        "wecom-channel",
        "slack-channel",
        "google-chat-channel",
        "discord-channel",
        "teams-channel",
        "whatsapp-channel",
    ],
)
@pytest.mark.parametrize("configured", [False, True])
async def test_channel_urls_require_settings_not_request_host(monkeypatch, url_actor, channel, configured):
    settings = Settings(PUBLIC_BASE_URL="https://public.example/" if configured else "")
    monkeypatch.setattr(platform_module, "get_settings", lambda: settings)
    for module in (slack, google_chat, teams):
        monkeypatch.setattr(module, "check_agent_access", AsyncMock())
    agent_id = uuid4()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.get(
            f"/api/agents/{agent_id}/{channel}/webhook-url",
            headers={"x-forwarded-host": "forged.example", "x-forwarded-proto": "https"},
        )

    if configured:
        assert response.status_code == 200
        assert response.json()["webhook_url"].startswith("https://public.example/api/channel/")
    else:
        assert response.status_code == 503
        assert "PUBLIC_BASE_URL" in response.json()["detail"]
        assert "webhook_url" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["https://ignored.example", "", None])
async def test_platform_setting_rejects_public_base_url_before_writes(monkeypatch, url_actor, value):
    get_setting = AsyncMock()
    update_tenant = AsyncMock()
    monkeypatch.setattr(enterprise.system_setting_dao, "get_by_key", get_setting)
    monkeypatch.setattr(enterprise.tenant_dao, "update", update_tenant)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.put(
            "/api/enterprise/system-settings/platform", json={"value": {"public_base_url": value}}
        )

    assert response.status_code == 422
    assert "PUBLIC_BASE_URL" in response.json()["detail"]
    get_setting.assert_not_awaited()
    update_tenant.assert_not_awaited()


@pytest.mark.asyncio
async def test_invitation_missing_base_reports_clear_error_before_creating_codes(monkeypatch, url_actor):
    url_actor.role = UserRole.ORG_ADMIN
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(system_email_service, "resolve_email_config_async", AsyncMock(return_value=object()))
    create = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(enterprise.invitation_code_dao, "create", create)
    monkeypatch.setattr(system_email_service, "send_company_invitation_email", send)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.post("/api/enterprise/invite-users", json={"emails": ["new@example.com"]})

    assert response.status_code == 503
    assert "PUBLIC_BASE_URL" in response.json()["detail"]
    create.assert_not_awaited()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_google_oauth_missing_base_reaches_shared_handler(monkeypatch, url_actor):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme", sso_domain="https://custom.example")
    provider = IdentityProviderRecord(
        id=uuid4(),
        provider_type="google_workspace",
        name="Google",
        tenant_id=tenant.id,
        config={"client_id": "client", "client_secret": "secret"},
    )
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(google_workspace, "get_google_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(google_workspace_oauth.tenant_dao, "get", AsyncMock(return_value=tenant))
    exchange = AsyncMock()
    monkeypatch.setattr(google_workspace.GoogleWorkspaceAuthProvider, "exchange_code_for_token", exchange)
    scan = SimpleNamespace(status="pending", tenant_id=tenant.id, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(sso_scan_session_dao, "get", AsyncMock(return_value=scan))
    state = google_workspace_oauth.sign_google_sso_state(uuid4(), provider.id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.get("/api/auth/google_workspace/callback", params={"code": "code", "state": state})

    assert response.status_code == 503
    assert "PUBLIC_BASE_URL" in response.json()["detail"]
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_sso_callback_config_requires_base_even_with_custom_tenant_domain(monkeypatch, url_actor):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme", sso_domain="https://custom.example")
    scan = SimpleNamespace(id=uuid4(), tenant_id=tenant.id, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(sso.sso_scan_session_dao, "get", AsyncMock(return_value=scan))
    monkeypatch.setattr(sso.identity_provider_dao, "list_active_sso_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr(sso.tenant_dao, "get", AsyncMock(return_value=tenant))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.get("/api/sso/config", params={"sid": str(scan.id)})

    assert response.status_code == 503
    assert "PUBLIC_BASE_URL" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_enabled", [False, True])
async def test_tenant_switch_requires_base_only_when_redirecting(monkeypatch, url_actor, redirect_enabled):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme", sso_domain="https://custom.example")
    target = UserRecord(id=uuid4(), identity_id=url_actor.identity_id, tenant_id=tenant.id, role=UserRole.MEMBER)
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(auth.user_dao, "get_by_identity_and_tenant", AsyncMock(return_value=target))
    monkeypatch.setattr(auth.tenant_dao, "get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        auth.system_setting_dao, "is_sso_custom_domain_redirect_enabled", AsyncMock(return_value=redirect_enabled)
    )
    monkeypatch.setattr(auth, "create_access_token", AsyncMock(return_value="switch-token"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://untrusted.example") as client:
        response = await client.post("/api/auth/switch-tenant", json={"tenant_id": str(tenant.id)})

    if redirect_enabled:
        assert response.status_code == 503
        assert "PUBLIC_BASE_URL" in response.json()["detail"]
    else:
        assert response.status_code == 200
        assert response.json()["access_token"] == "switch-token"
        assert response.json()["redirect_url"] is None
