"""Tests for platform-admin tenant creation with a genesis org admin."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import tenants as tenants_api
from app.records.tenant import TenantRecord
from app.services.tenant_provisioning import (
    AdminEmailTakenError,
    ProvisionedTenant,
    create_tenant_with_org_admin,
    slugify_tenant_name,
)


def _platform_user():
    return SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=None, identity=None)


def _member_user():
    return SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4(), identity=None)


def _tenant_record(*, name="Acme", slug="acme-abc123"):
    return TenantRecord(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        im_provider="web_only",
        is_active=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_create_tenant_rejects_non_platform_admin():
    with pytest.raises(HTTPException) as exc:
        await tenants_api.create_tenant(
            tenants_api.TenantCreate(
                name="Acme",
                admin_email="orgadmin@acme.com",
                admin_password="temp-password",
            ),
            current_user=_member_user(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_tenant_provisions_org_admin(monkeypatch):
    tenant = _tenant_record()
    provisioned = ProvisionedTenant(
        tenant=tenant,
        org_admin=SimpleNamespace(id=uuid.uuid4(), role="org_admin"),
        admin_email="orgadmin@acme.com",
    )
    create = AsyncMock(return_value=provisioned)
    monkeypatch.setattr("app.api.tenants.create_tenant_with_org_admin", create)

    result = await tenants_api.create_tenant(
        tenants_api.TenantCreate(
            name="Acme",
            admin_email="orgadmin@acme.com",
            admin_password="temp-password",
            admin_display_name="Org Admin",
        ),
        current_user=_platform_user(),
    )

    assert result.org_admin_email == "orgadmin@acme.com"
    assert result.must_change_password is True
    assert result.tenant.id == tenant.id
    assert result.tenant.name == "Acme"
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["name"] == "Acme"
    assert kwargs["admin_email"] == "orgadmin@acme.com"
    assert kwargs["admin_password"] == "temp-password"
    assert kwargs["admin_display_name"] == "Org Admin"


@pytest.mark.asyncio
async def test_create_tenant_rejects_existing_admin_email(monkeypatch):
    monkeypatch.setattr(
        "app.api.tenants.create_tenant_with_org_admin",
        AsyncMock(side_effect=AdminEmailTakenError("taken@example.com")),
    )

    with pytest.raises(HTTPException) as exc:
        await tenants_api.create_tenant(
            tenants_api.TenantCreate(
                name="Acme",
                admin_email="taken@example.com",
                admin_password="temp-password",
            ),
            current_user=_platform_user(),
        )
    assert exc.value.status_code == 409


def test_slugify_tenant_name_appends_unique_suffix():
    slug = slugify_tenant_name("Acme Corp")
    assert slug.startswith("acme-corp-")
    assert len(slug.split("-")[-1]) == 6


@pytest.mark.asyncio
async def test_create_tenant_with_org_admin_write_kwargs(monkeypatch):
    tenant_id = uuid.uuid4()
    identity_id = uuid.uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        name="Acme",
        slug="acme-abc",
        is_active=True,
        created_at=None,
        default_message_limit=50,
        default_message_period="permanent",
        default_max_agents=2,
        default_agent_ttl_hours=0,
    )
    identity = SimpleNamespace(
        id=identity_id,
        email="orgadmin@acme.com",
        username="orgadmin",
        must_change_password=True,
    )
    org_admin = SimpleNamespace(
        id=uuid.uuid4(),
        identity_id=identity_id,
        tenant_id=tenant_id,
        display_name="Org Admin",
        avatar_url=None,
        role="org_admin",
        identity=None,
    )
    create_identity = AsyncMock(return_value=identity)
    create_user = AsyncMock(return_value=org_admin)
    monkeypatch.setattr("app.services.tenant_provisioning.identity_dao.get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.tenant_provisioning.identity_dao.is_username_taken", AsyncMock(return_value=False)
    )
    monkeypatch.setattr("app.services.tenant_provisioning.hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr("app.services.tenant_provisioning.tenant_dao.create", AsyncMock(return_value=tenant))
    monkeypatch.setattr("app.services.tenant_provisioning.identity_dao.create_identity", create_identity)
    monkeypatch.setattr("app.services.tenant_provisioning.user_dao.create", create_user)
    monkeypatch.setattr("app.services.tenant_provisioning.participant_dao.create_for_user", AsyncMock())

    bind = AsyncMock()
    from unittest.mock import patch

    with (
        patch("app.services.tenant_provisioning.connection_ctx") as ctx,
        patch("app.services.registration_service.registration_service.bind_org_member", bind),
    ):
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await create_tenant_with_org_admin(
            name="Acme",
            admin_email="OrgAdmin@acme.com",
            admin_password="temp-password",
            admin_display_name="Org Admin",
        )

    assert result.admin_email == "orgadmin@acme.com"
    assert result.tenant is tenant
    kwargs = create_identity.await_args.kwargs
    assert kwargs["email"] == "orgadmin@acme.com"
    assert kwargs["must_change_password"] is True
    assert kwargs["is_platform_admin"] is False
    user_kwargs = create_user.await_args.kwargs["obj_in"]
    assert user_kwargs["role"] == "org_admin"
    assert user_kwargs["registration_source"] == "platform_admin"
    bind.assert_awaited_once()
    assert bind.await_args.args[0].identity is identity


@pytest.mark.asyncio
async def test_create_tenant_with_org_admin_rejects_existing_email(monkeypatch):
    monkeypatch.setattr(
        "app.services.tenant_provisioning.identity_dao.get_by_email",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )

    with pytest.raises(AdminEmailTakenError):
        await create_tenant_with_org_admin(
            name="Acme",
            admin_email="taken@example.com",
            admin_password="temp-password",
        )
