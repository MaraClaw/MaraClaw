"""Tests for platform-admin tenant creation with a genesis org admin."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import tenants as tenants_api
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.services import tenant_provisioning
from app.services.tenant_provisioning import (
    AdminEmailTakenError,
    ProvisionedTenant,
    create_tenant_with_org_admin,
    slugify_tenant_name,
)

PHONE = "+84912345678"


def _platform_user():
    return SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=None, identity=None)


def _member_user():
    return SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4(), identity=None)


def _tenant_record(*, name="Acme", slug="acme-abc123", tier=OrganizationPlanId.PRO):
    return TenantRecord(
        id=uuid.uuid4(),
        name=name,
        slug=slug,
        im_provider="web_only",
        is_active=True,
        created_at=datetime.now(UTC),
        tier=tier,
        phone=PHONE,
    )


@pytest.mark.asyncio
async def test_create_tenant_rejects_non_platform_admin():
    with pytest.raises(HTTPException) as exc:
        await tenants_api.create_tenant(
            tenants_api.TenantCreate(
                name="Acme",
                tier=OrganizationPlanId.PRO,
                phone=PHONE,
                admin_email="orgadmin@acme.com",
                admin_password="temp-password",
            ),
            current_user=_member_user(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_create_tenant_provisions_org_admin(monkeypatch):
    tenant = _tenant_record(tier=OrganizationPlanId.ULTRA)
    org_admin: Any = SimpleNamespace(id=uuid.uuid4(), role="org_admin")
    provisioned = ProvisionedTenant(
        tenant=tenant,
        org_admin=org_admin,
        admin_email="orgadmin@acme.com",
    )
    create = AsyncMock(return_value=provisioned)
    audit = AsyncMock()
    platform_user = _platform_user()
    monkeypatch.setattr("app.api.tenants.create_tenant_with_org_admin", create)
    monkeypatch.setattr("app.api.tenants.write_admin_audit", audit)

    result = await tenants_api.create_tenant(
        tenants_api.TenantCreate(
            name="Acme",
            tier=OrganizationPlanId.ULTRA,
            phone="+84 912 345 678",
            admin_email="orgadmin@acme.com",
            admin_password="temp-password",
            admin_display_name="Org Admin",
        ),
        current_user=platform_user,
    )

    assert result.org_admin_email == "orgadmin@acme.com"
    assert result.must_change_password is True
    assert result.tenant.id == tenant.id
    assert result.tenant.name == "Acme"
    create.assert_awaited_once_with(
        tier=OrganizationPlanId.ULTRA,
        name="Acme",
        phone=PHONE,
        admin_email="orgadmin@acme.com",
        admin_password="temp-password",
        admin_display_name="Org Admin",
    )
    assert audit.await_args is not None
    assert audit.await_args.kwargs["changes"]["tier"] == {"before": None, "after": "ultra"}
    assert audit.await_args.kwargs["changes"]["phone"] == {"before": None, "after": PHONE}


@pytest.mark.parametrize("fields", [{}, {"tier": "enterprise"}, {"tier": None}])
def test_tenant_create_requires_a_known_tier(fields):
    with pytest.raises(ValidationError) as exc:
        _ = tenants_api.TenantCreate.model_validate(
            {
                "name": "Acme",
                "phone": PHONE,
                "admin_email": "orgadmin@acme.com",
                "admin_password": "temp-password",
                **fields,
            }
        )
    assert [error["loc"] for error in exc.value.errors()] == [("tier",)]


@pytest.mark.parametrize("fields", [{}, {"phone": None}, {"phone": "+84123"}, {"phone": "0912345678"}])
def test_tenant_create_requires_a_valid_phone(fields):
    with pytest.raises(ValidationError) as exc:
        _ = tenants_api.TenantCreate.model_validate(
            {
                "name": "Acme",
                "tier": "pro",
                "admin_email": "orgadmin@acme.com",
                "admin_password": "temp-password",
                **fields,
            }
        )
    assert [error["loc"] for error in exc.value.errors()] == [("phone",)]


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
                tier=OrganizationPlanId.PRO,
                phone=PHONE,
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
    monkeypatch.setattr("app.services.tenant_provisioning.hash_password_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr("app.services.tenant_provisioning.tenant_dao.create", AsyncMock(return_value=tenant))
    monkeypatch.setattr("app.services.tenant_provisioning.identity_dao.create_identity", create_identity)
    monkeypatch.setattr("app.services.tenant_provisioning.user_dao.create", create_user)
    monkeypatch.setattr("app.services.tenant_provisioning.participant_dao.create_for_user", AsyncMock())

    bind = AsyncMock()
    claim = AsyncMock()
    from unittest.mock import patch

    with (
        patch("app.services.tenant_provisioning.connection_ctx") as ctx,
        patch("app.services.registration_service.registration_service.bind_org_member", bind),
        patch("app.services.tenant_provisioning._claim_genesis_admin_email_domain", claim),
    ):
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await create_tenant_with_org_admin(
            tier=OrganizationPlanId.MAX,
            name="Acme",
            phone=PHONE,
            admin_email="OrgAdmin@acme.com",
            admin_password="temp-password",
            admin_display_name="Org Admin",
        )

    assert result.admin_email == "orgadmin@acme.com"
    assert result.tenant is tenant
    create_tenant = tenant_provisioning.tenant_dao.create
    assert create_tenant.await_args is not None
    assert create_tenant.await_args.kwargs["obj_in"]["tier"] is OrganizationPlanId.MAX
    assert create_tenant.await_args.kwargs["obj_in"]["phone"] == PHONE
    kwargs = create_identity.await_args.kwargs
    assert kwargs["email"] == "orgadmin@acme.com"
    assert kwargs["must_change_password"] is True
    assert kwargs["is_platform_admin"] is False
    user_kwargs = create_user.await_args.kwargs["obj_in"]
    assert user_kwargs["role"] == "org_admin"
    assert user_kwargs["registration_source"] == "platform_admin"
    assert user_kwargs["is_genesis"] is True
    bind.assert_awaited_once()
    assert bind.await_args.args[0].identity is identity
    claim.assert_awaited_once_with(tenant_id, "orgadmin@acme.com")


@pytest.mark.asyncio
async def test_claim_genesis_admin_email_domain_defaults_host(monkeypatch):
    from app.services.tenant_provisioning import _claim_genesis_admin_email_domain

    add = AsyncMock()
    monkeypatch.setattr("app.services.org_membership.add_email_domain", add)
    tenant_id = uuid.uuid4()
    await _claim_genesis_admin_email_domain(tenant_id, "techadmin@marathon.vn")
    add.assert_awaited_once_with(tenant_id, "marathon.vn", is_default=True)


@pytest.mark.asyncio
async def test_claim_genesis_admin_email_domain_skips_invalid(monkeypatch):
    from app.services.tenant_provisioning import _claim_genesis_admin_email_domain

    add = AsyncMock()
    monkeypatch.setattr("app.services.org_membership.add_email_domain", add)
    await _claim_genesis_admin_email_domain(uuid.uuid4(), "not-an-email")
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_tenant_rejects_claimed_admin_domain(monkeypatch):
    from app.services.org_membership import DomainClaimedError

    monkeypatch.setattr(
        "app.api.tenants.create_tenant_with_org_admin",
        AsyncMock(side_effect=DomainClaimedError("Email domain is already claimed")),
    )
    with pytest.raises(HTTPException) as exc:
        await tenants_api.create_tenant(
            tenants_api.TenantCreate(
                name="Acme",
                tier=OrganizationPlanId.PRO,
                phone=PHONE,
                admin_email="techadmin@marathon.vn",
                admin_password="temp-password",
            ),
            current_user=_platform_user(),
        )
    assert exc.value.status_code == 409
    assert "domain" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_create_tenant_with_org_admin_rejects_existing_email(monkeypatch):
    monkeypatch.setattr(
        "app.services.tenant_provisioning.identity_dao.get_by_email",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )

    with pytest.raises(AdminEmailTakenError):
        await create_tenant_with_org_admin(
            tier=OrganizationPlanId.PRO,
            name="Acme",
            phone=PHONE,
            admin_email="taken@example.com",
            admin_password="temp-password",
        )


@pytest.mark.asyncio
async def test_attach_genesis_org_admin_creates_when_missing(monkeypatch):
    from app.services.tenant_provisioning import attach_genesis_org_admin

    tenant_id = uuid.uuid4()
    tenant = SimpleNamespace(
        id=tenant_id,
        default_message_limit=50,
        default_message_period="permanent",
        default_max_agents=2,
        default_agent_ttl_hours=0,
    )
    identity = SimpleNamespace(id=uuid.uuid4())
    org_admin = SimpleNamespace(
        id=uuid.uuid4(),
        display_name="OA",
        avatar_url=None,
        identity=None,
    )
    create_user = AsyncMock(return_value=org_admin)
    monkeypatch.setattr("app.services.tenant_provisioning.tenant_dao.get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        "app.services.tenant_provisioning.user_dao.genesis_org_admin_for_tenant", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.services.tenant_provisioning.identity_dao.get_by_email", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.tenant_provisioning.hash_password_async", AsyncMock(return_value="hashed"))
    create_identity = AsyncMock(return_value=identity)
    monkeypatch.setattr("app.services.tenant_provisioning.identity_dao.create_identity", create_identity)
    monkeypatch.setattr("app.services.tenant_provisioning.user_dao.create", create_user)
    participant = AsyncMock()
    monkeypatch.setattr("app.services.tenant_provisioning.participant_dao.create_for_user", participant)
    claim = AsyncMock()
    monkeypatch.setattr("app.services.org_membership.add_email_domain", claim)
    bind = AsyncMock()
    with (
        patch("app.services.tenant_provisioning.connection_ctx") as ctx,
        patch("app.services.registration_service.registration_service.bind_org_member", bind),
    ):
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        result = await attach_genesis_org_admin(
            tenant_id=tenant_id,
            admin_email="oa@acme.com",
            admin_password="temp-password",
        )
    assert result.admin_email == "oa@acme.com"
    assert create_user.await_args is not None
    user_fields = create_user.await_args.kwargs["obj_in"]
    assert (user_fields["role"], user_fields["is_genesis"], user_fields["tenant_id"]) == ("org_admin", True, tenant_id)
    assert create_identity.await_args is not None
    identity_fields = create_identity.await_args.kwargs
    assert identity_fields["must_change_password"] is True
    assert identity_fields["is_platform_admin"] is False
    participant.assert_awaited_once()
    bind.assert_awaited_once_with(org_admin)
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_genesis_org_admin_conflict(monkeypatch):
    from app.services.tenant_provisioning import GenesisOrgAdminExistsError, attach_genesis_org_admin

    tenant = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr("app.services.tenant_provisioning.tenant_dao.get", AsyncMock(return_value=tenant))
    monkeypatch.setattr(
        "app.services.tenant_provisioning.user_dao.genesis_org_admin_for_tenant",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )
    with pytest.raises(GenesisOrgAdminExistsError):
        await attach_genesis_org_admin(
            tenant_id=tenant.id,
            admin_email="oa@acme.com",
            admin_password="temp-password",
        )


@pytest.mark.asyncio
async def test_delete_tenant_and_release_identities(monkeypatch):
    from app.services.tenant_provisioning import delete_tenant_and_release_identities

    tenant_id = uuid.uuid4()
    ids = [uuid.uuid4()]
    monkeypatch.setattr("app.services.tenant_provisioning.assert_tenant_deletion_allowed", AsyncMock())
    monkeypatch.setattr(
        "app.services.tenant_provisioning.user_dao.list_identity_ids_for_tenant", AsyncMock(return_value=ids)
    )
    cascade = AsyncMock()
    tombstone = AsyncMock()
    monkeypatch.setattr("app.services.tenant_provisioning.agent_dao.list_for_tenant", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.tenant_provisioning.tenant_dao.delete_cascade", cascade)
    monkeypatch.setattr("app.services.tenant_provisioning.identity_dao.tombstone_orphans", tombstone)
    with patch("app.services.tenant_provisioning.connection_ctx") as ctx:
        ctx.return_value.__aenter__ = AsyncMock(return_value=None)
        ctx.return_value.__aexit__ = AsyncMock(return_value=None)
        await delete_tenant_and_release_identities(tenant_id)
    cascade.assert_awaited_once_with(tenant_id)
    tombstone.assert_awaited_once_with(ids)
