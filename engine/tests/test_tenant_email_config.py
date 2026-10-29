"""Tenant SMTP routing, validation and secret storage contracts."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.dao.tenant_dao import tenant_dao
from app.records.tenant import TenantRecord
from app.schemas.tenant_email import TenantEmailUpdate
from app.services import email_config


@pytest.mark.parametrize("display_name", [None, "", "Organization relay"])
def test_tenant_sender_name_preserves_empty_or_explicit_value(display_name):
    # Given a tenant draft with an omitted, empty or explicit sender display name.
    payload = {"enabled": True, "smtp_host": "smtp.example.com", "from_address": "org@example.com"}
    if display_name is not None:
        payload["from_name"] = display_name
    draft = TenantEmailUpdate.model_validate(payload)
    # When the draft is stored, loaded and converted into a delivery transport.
    settings = email_config.load_settings(email_config.stored_settings(draft, {}, public_only=True))
    transport = email_config.transport(settings)
    # Then no tenant-specific product display name is injected.
    assert settings.from_name == (display_name or "")
    assert transport is not None
    assert transport.from_name == (display_name or "")


@pytest.mark.parametrize("password", [None, "", "new-secret"])
def test_password_is_encrypted_and_preserved_when_blank(password):
    # Given an existing encrypted credential.
    prior = email_config.stored_settings(TenantEmailUpdate(password="old-secret"), {}, public_only=True)
    # When a draft is replaced without a meaningful new password.
    saved = email_config.stored_settings(TenantEmailUpdate(password=password), prior, public_only=True)
    # Then storage never exposes plaintext and loading retains the intended password.
    assert saved["password"] not in ("old-secret", "new-secret")
    loaded = email_config.load_settings(saved)
    assert loaded.password == (password or "old-secret")


@pytest.mark.parametrize(
    "draft",
    [
        {"enabled": True},
        {"enabled": True, "smtp_host": "smtp.example.com", "from_address": "invalid"},
        {"smtp_port": 0},
        {"smtp_port": 65536},
        {"smtp_timeout_seconds": 0},
        {"smtp_timeout_seconds": 31},
        {"smtp_host": "https://smtp.example.com"},
        {"from_name": "Name\r\nBcc: victim@example.com"},
    ],
)
def test_invalid_drafts_are_rejected(draft):
    # Given an invalid boundary payload, when parsed, then reject it.
    with pytest.raises(ValidationError):
        TenantEmailUpdate.model_validate(draft)


@pytest.mark.asyncio
async def test_explicit_tenant_never_consults_maraclaw(monkeypatch):
    # Given distinct tenant and platform transports.
    tenant_id = uuid4()
    saved = TenantEmailUpdate(enabled=True, smtp_host="tenant.example.com", from_address="org@example.com")

    async def get(tenant):
        assert tenant == tenant_id
        return email_config.stored_settings(saved, {}, public_only=True)

    mara_get = AsyncMock(side_effect=AssertionError("Explicit routing must not consult MaraClaw"))
    monkeypatch.setattr(email_config.tenant_email_dao, "get", get)
    monkeypatch.setattr(tenant_dao, "get_by_slug", mara_get)
    # When explicit resolution is requested.
    config, source = await email_config.resolve_with_source(tenant_id)
    # Then only the tenant transport is selected.
    assert source == "tenant"
    assert config is not None
    assert config.smtp_host == "tenant.example.com"
    mara_get.assert_not_called()


@pytest.mark.parametrize("state", ["enabled", "missing", "disabled", "incomplete", "not_system"])
async def test_noarg_resolution_uses_only_saved_maraclaw(monkeypatch, state):
    # Given MaraClaw and distinct KABERs settings that cannot become the default.
    mara = TenantRecord(uuid4(), "MaraClaw", "maraclaw", is_system=state != "not_system")
    saved = email_config.stored_settings(
        TenantEmailUpdate(enabled=state != "disabled", smtp_host="mara.example.com", from_address="mara@example.com"),
        {},
        public_only=False,
    )
    if state == "incomplete":
        saved["smtp_host"] = ""

    async def get_slug(slug):
        assert slug == "maraclaw"
        return None if state == "missing" else mara

    async def get(tenant_id):
        assert tenant_id == mara.id
        return saved

    monkeypatch.setattr(tenant_dao, "get_by_slug", get_slug)
    tenant_get = AsyncMock(side_effect=get)
    monkeypatch.setattr(email_config.tenant_email_dao, "get", tenant_get)
    # When identity-level delivery resolves its default transport.
    config = await email_config.resolve_email_config_async()
    # Then only enabled, complete settings belonging to system MaraClaw qualify.
    if state == "enabled":
        assert config is not None
        assert config.smtp_host == "mara.example.com"
    else:
        assert config is None
    if state in ("missing", "not_system"):
        tenant_get.assert_not_called()


@pytest.mark.parametrize("saved", [{}, {"enabled": False}, {"enabled": True}, {"smtp_port": "corrupt"}])
async def test_unusable_tenant_never_falls_back_to_maraclaw(monkeypatch, saved):
    # Given missing, disabled, incomplete or corrupt tenant data and a separate default organization.
    monkeypatch.setattr(email_config.tenant_email_dao, "get", AsyncMock(return_value=saved))
    mara_get = AsyncMock(side_effect=AssertionError("Unusable tenant configuration must not consult MaraClaw"))
    monkeypatch.setattr(tenant_dao, "get_by_slug", mara_get)
    # When the explicit tenant is resolved.
    config, source = await email_config.resolve_with_source(uuid4())
    # Then no SMTP transport is selected and platform secrets are not even loaded.
    assert (config, source) == (None, "none")
    mara_get.assert_not_called()
