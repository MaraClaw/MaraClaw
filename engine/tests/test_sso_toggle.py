from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from app.api import admin as admin_api, sso as sso_api, tenants as tenants_api
from app.services.platform_service import platform_service


class DummyResult:
    def __init__(self, values: list[SimpleNamespace] | None = None):
        self._values = values or []

    def scalar_one_or_none(self) -> SimpleNamespace | None:
        return self._values[0] if self._values else None

    def scalars(self) -> DummyResult:
        return self

    def unique(self) -> DummyResult:
        return self

    def one(self) -> SimpleNamespace:
        return self._values[0]

    def all(self) -> list[SimpleNamespace]:
        return self._values.copy()


@dataclass(slots=True)
class SsoTenant:
    slug: str
    sso_domain: str | None


@pytest.mark.asyncio
async def test_get_platform_settings_sso_toggle_default(monkeypatch):
    """Verify that get_platform_settings returns sso_custom_domain_redirect_enabled by default."""
    from app.dao import system_setting_dao

    async def is_flag_enabled(_key: str, default: bool = False) -> bool:
        return default

    monkeypatch.setattr(system_setting_dao, "is_flag_enabled", is_flag_enabled)
    current_user = MagicMock()
    settings = await admin_api.get_platform_settings(current_user=current_user)

    assert settings.sso_custom_domain_redirect_enabled is True
    assert settings.allow_self_create_company is True
    assert settings.invitation_code_enabled is False


@pytest.mark.asyncio
async def test_get_platform_settings_sso_toggle_disabled(monkeypatch):
    """Verify that get_platform_settings returns sso_custom_domain_redirect_enabled False if set."""
    from app.dao import system_setting_dao

    async def is_flag_enabled(key: str, default: bool = False) -> bool:
        if key == "sso_custom_domain_redirect_enabled":
            return False
        return default

    monkeypatch.setattr(system_setting_dao, "is_flag_enabled", is_flag_enabled)
    current_user = MagicMock()
    settings = await admin_api.get_platform_settings(current_user=current_user)
    assert settings.sso_custom_domain_redirect_enabled is False


@pytest.mark.asyncio
async def test_resolve_tenant_by_domain_sso_toggle(monkeypatch):
    """Verify that resolve_tenant_by_domain respects the sso_custom_domain_redirect_enabled toggle."""
    from app.dao import system_setting_dao, tenant_dao

    active_tenant = SimpleNamespace(
        id="tenant-id", name="Acme", slug="acme", sso_enabled=True, sso_domain="https://acme.com", is_active=True
    )

    async def sso_enabled(_key: str, default: bool = False) -> bool:
        return True

    async def get_by_sso_domain_exact(domain: str):
        if domain == "https://acme.com":
            return active_tenant
        return None

    async def get_by_sso_domain_like(_domain: str):
        return None

    async def get_by_slug(_slug: str):
        return None

    monkeypatch.setattr(system_setting_dao, "is_flag_enabled", sso_enabled)
    monkeypatch.setattr(tenant_dao, "get_by_sso_domain_exact", get_by_sso_domain_exact)
    monkeypatch.setattr(tenant_dao, "get_by_sso_domain_like", get_by_sso_domain_like)
    monkeypatch.setattr(tenant_dao, "get_by_slug", get_by_slug)

    res = await tenants_api.resolve_tenant_by_domain(domain="acme.com")
    assert res["id"] == "tenant-id"
    assert res["sso_domain"] == "https://acme.com"

    async def sso_disabled(_key: str, default: bool = False) -> bool:
        return False

    monkeypatch.setattr(system_setting_dao, "is_flag_enabled", sso_disabled)
    with pytest.raises(HTTPException) as exc:
        await tenants_api.resolve_tenant_by_domain(domain="acme.com")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_tenant_sso_base_url_toggle():
    """Verify that get_tenant_sso_base_url respects the sso_redirect_enabled kwarg."""
    tenant = SsoTenant(slug="acme", sso_domain="https://acme.com")

    # 1. Enabled: returns the custom sso_domain
    url = await platform_service.get_tenant_sso_base_url(tenant=tenant, sso_redirect_enabled=True)
    assert url == "https://acme.com"

    # 2. Disabled: falls back to public base URL
    with patch.object(platform_service, "get_public_base_url", return_value="https://try.maraclaw.ai"):
        url = await platform_service.get_tenant_sso_base_url(tenant=tenant, sso_redirect_enabled=False)
        assert url == "https://try.maraclaw.ai"


@pytest.mark.asyncio
async def test_sso_config_uses_public_callback_for_unresolved_tenant(monkeypatch):
    """Use the public callback URL when a soft-linked tenant has been deleted."""
    import uuid
    from datetime import UTC, datetime, timedelta

    from app.dao.identity_provider_dao import identity_provider_dao
    from app.dao.sso_scan_session_dao import sso_scan_session_dao
    from app.dao.tenant_dao import tenant_dao

    tenant_id = uuid.uuid4()
    sid = uuid.uuid4()
    session = SimpleNamespace(
        id=sid,
        tenant_id=tenant_id,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    provider = SimpleNamespace(
        provider_type="feishu",
        config={"app_id": "scoped-feishu-app"},
        name="Scoped Feishu",
    )
    request = MagicMock(spec=Request)

    async def get_session(session_id):
        assert session_id == sid
        return session

    async def list_providers(tid):
        assert tid == tenant_id
        return [provider]

    async def get_tenant(tid):
        assert tid == tenant_id
        return  # soft-linked tenant deleted

    monkeypatch.setattr(sso_scan_session_dao, "get", get_session)
    monkeypatch.setattr(identity_provider_dao, "list_active_sso_for_tenant", list_providers)
    monkeypatch.setattr(tenant_dao, "get", get_tenant)

    with (
        patch.object(
            platform_service,
            "get_public_base_url",
            new_callable=AsyncMock,
            return_value="https://public.example",
        ) as public_url,
        patch.object(platform_service, "get_tenant_sso_base_url", new_callable=AsyncMock) as tenant_url,
    ):
        auth_urls = await sso_api.get_sso_config(sid=sid, request=request)

    tenant_url.assert_not_awaited()
    public_url.assert_awaited_once_with(None, request)
    assert auth_urls[0]["url"].startswith("https://open.feishu.cn/open-apis/authen/v1/index?app_id=scoped-feishu-app")
    assert "redirect_uri=https%3A//public.example/api/auth/feishu/callback" in auth_urls[0]["url"]


@pytest.mark.asyncio
async def test_switch_tenant_sso_toggle(monkeypatch):
    """Verify that switch_tenant API respects the sso_custom_domain_redirect_enabled toggle."""
    import uuid

    from app.api import auth as auth_api
    from app.dao import system_setting_dao, tenant_dao, user_dao
    from app.schemas.schemas import TenantSwitchRequest

    target_tenant_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    target_user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=target_tenant_id, is_active=True)
    tenant = SimpleNamespace(id=target_tenant_id, slug="acme", sso_domain="https://acme.com", is_active=True)
    current_user = SimpleNamespace(identity_id=identity_id, display_name="Current User", id=uuid.uuid4())
    data = TenantSwitchRequest(tenant_id=target_tenant_id)
    request = MagicMock()

    async def get_by_identity_and_tenant(id_, tenant_id):
        assert id_ == identity_id
        assert tenant_id == target_tenant_id
        return target_user

    async def get_tenant(tid):
        assert tid == target_tenant_id
        return tenant

    monkeypatch.setattr(user_dao, "get_by_identity_and_tenant", get_by_identity_and_tenant)
    monkeypatch.setattr(tenant_dao, "get", get_tenant)

    # Case 1: Toggle enabled -> redirect_url is returned
    async def sso_enabled():
        return True

    monkeypatch.setattr(system_setting_dao, "is_sso_custom_domain_redirect_enabled", sso_enabled)
    with patch("app.api.auth.create_access_token", return_value="jwt-token"):
        res = await auth_api.switch_tenant(data, request, current_user)
        assert res.access_token == "jwt-token"
        assert res.redirect_url is not None
        assert "https://acme.com" in res.redirect_url

    # Case 2: Toggle disabled -> redirect_url is None
    async def sso_disabled():
        return False

    monkeypatch.setattr(system_setting_dao, "is_sso_custom_domain_redirect_enabled", sso_disabled)
    with patch("app.api.auth.create_access_token", return_value="jwt-token"):
        res = await auth_api.switch_tenant(data, request, current_user)
        assert res.access_token == "jwt-token"
        assert res.redirect_url is None
