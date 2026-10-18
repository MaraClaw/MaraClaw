from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api import enterprise
from app.config import Settings
from app.records.tenant import TenantRecord
from app.services import platform_service as platform_module
from app.services.sso_service import sso_service


@pytest.mark.asyncio
@pytest.mark.parametrize("active_count", [0, 1])
async def test_sso_state_updates_enabled_without_persisting_missing_base(monkeypatch, active_count):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=""))
    monkeypatch.setattr(enterprise.identity_provider_dao, "count_active_sso", AsyncMock(return_value=active_count))
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=tenant))
    update = AsyncMock()
    clear = AsyncMock()
    monkeypatch.setattr(enterprise.tenant_dao, "update", update)
    monkeypatch.setattr(enterprise.tenant_dao, "clear_sso_domain_except", clear)

    await enterprise._sync_tenant_sso_state(tenant.id)

    update.assert_awaited_once_with(db_obj=tenant, obj_in={"sso_enabled": active_count > 0})
    clear.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base", "domain", "clears_others"),
    [("https://app.example.com", "https://acme.example.com", False), ("http://192.0.2.1", "http://192.0.2.1", True)],
)
async def test_sso_state_persists_only_configured_domain(monkeypatch, base, domain, clears_others):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=base))
    monkeypatch.setattr(enterprise.identity_provider_dao, "count_active_sso", AsyncMock(return_value=1))
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=tenant))
    update = AsyncMock()
    clear = AsyncMock()
    monkeypatch.setattr(enterprise.tenant_dao, "update", update)
    monkeypatch.setattr(enterprise.tenant_dao, "clear_sso_domain_except", clear)

    await enterprise._sync_tenant_sso_state(tenant.id)

    update.assert_awaited_once_with(db_obj=tenant, obj_in={"sso_enabled": True, "sso_domain": domain})
    assert clear.await_count == int(clears_others)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["", "https://app.example.com"])
async def test_sso_enablement_avoids_ip_conflict_lookup_without_configured_ip(monkeypatch, base):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL=base))
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=tenant))
    conflicts = AsyncMock(return_value=[SimpleNamespace(tenant_id=uuid4())])
    monkeypatch.setattr(enterprise.identity_provider_dao, "list_active_sso_excluding_tenant", conflicts)

    allowed = await sso_service.validate_sso_enablement(tenant.id)

    assert allowed is True
    conflicts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_conflict", [False, True])
async def test_sso_enablement_checks_conflicts_for_configured_ip(monkeypatch, has_conflict):
    tenant = TenantRecord(id=uuid4(), name="Acme", slug="acme")
    monkeypatch.setattr(platform_module, "get_settings", lambda: Settings(PUBLIC_BASE_URL="http://192.0.2.1"))
    monkeypatch.setattr(enterprise.tenant_dao, "get", AsyncMock(return_value=tenant))
    conflicts = AsyncMock(return_value=[SimpleNamespace(tenant_id=uuid4())] if has_conflict else [])
    monkeypatch.setattr(enterprise.identity_provider_dao, "list_active_sso_excluding_tenant", conflicts)

    allowed = await sso_service.validate_sso_enablement(tenant.id)

    assert allowed is (not has_conflict)
    conflicts.assert_awaited_once_with(tenant.id)
