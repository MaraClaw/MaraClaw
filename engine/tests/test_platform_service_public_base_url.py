from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import Request

from app.config import Settings, get_settings
from app.records.tenant import TenantRecord
from app.services import platform_service as platform_module
from app.services.platform_service import PublicBaseUrlNotConfiguredError, platform_service


@pytest.mark.asyncio
async def test_public_base_url_loads_dotenv_without_export(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PUBLIC_BASE_URL=https://dotenv.example/\n", encoding="utf-8")
    get_settings.cache_clear()
    try:
        resolved = await platform_service.get_public_base_url()

        assert resolved == "https://dotenv.example"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_request", [False, True])
@pytest.mark.parametrize("configured", ["", "  "])
async def test_public_base_url_requires_settings_even_with_request(monkeypatch, with_request, configured) -> None:
    settings = Settings(PUBLIC_BASE_URL=configured)
    monkeypatch.setattr(platform_module, "get_settings", lambda: settings)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://unowned.example")
    request = Request({"type": "http", "headers": [(b"host", b"untrusted.example")], "scheme": "http"})

    with pytest.raises(PublicBaseUrlNotConfiguredError, match="PUBLIC_BASE_URL"):
        await platform_service.get_public_base_url(request if with_request else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", [None, "https://custom.example"])
@pytest.mark.parametrize("redirect_enabled", [False, True])
async def test_tenant_sso_base_url_propagates_missing_settings(monkeypatch, domain, redirect_enabled) -> None:
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme", sso_domain=domain)

    with pytest.raises(PublicBaseUrlNotConfiguredError):
        await platform_service.get_tenant_sso_base_url(tenant, sso_redirect_enabled=redirect_enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (" https://app.example.com/ ", "https://acme.example.com"),
        ("http://192.0.2.1:8000/", "http://192.0.2.1:8000"),
        ("http://localhost:8000/", "http://localhost:8000"),
    ],
)
async def test_tenant_sso_base_url_uses_configured_host(monkeypatch, configured, expected) -> None:
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=configured))
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")

    resolved = await platform_service.get_tenant_sso_base_url(tenant)

    assert resolved == expected
