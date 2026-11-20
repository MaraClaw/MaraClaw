"""System organization seeder."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest
from tests.transaction_pool_fakes import CountingPool

from app.db import session
from app.records.organization_plan import OrganizationPlanId
from app.services import system_org_seeder as seeder
from app.services.tenant_provisioning import AdminEmailTakenError, GenesisOrgAdminExistsError

ATTACH = "app.services.tenant_provisioning.attach_genesis_org_admin"


def _tenant(**overrides: Any) -> Any:
    values = {
        "id": uuid.uuid4(),
        "name": "KABERs",
        "slug": "kabers",
        "tier": OrganizationPlanId.PRO,
        "is_system": True,
        "is_active": True,
        "is_default_end_user_org": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _settings(email: str, password: str):
    return SimpleNamespace(KABERS_ORG_ADMIN_EMAIL=email, KABERS_ORG_ADMIN_PASSWORD=password)


def _valid_settings():
    return _settings("owner@example.com", "initial-secret")


class _TenantStore:
    """In-memory tenant rows keyed by slug, mirroring the DAO calls the seeder makes."""

    def __init__(self, *rows):
        self.rows = {row.slug: row for row in rows}
        self.created = []
        self.lock = AsyncMock()
        self.seed_lock = AsyncMock()

    def install(self, monkeypatch):
        monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", self.get_by_slug)
        monkeypatch.setattr(seeder.tenant_dao, "create", self.create)
        monkeypatch.setattr(seeder.tenant_dao, "update", self.update)
        monkeypatch.setattr(seeder.tenant_dao, "tiers_for", self.tiers_for)
        monkeypatch.setattr(seeder.tenant_dao, "get_default_end_user_org", self.get_default_end_user_org)
        monkeypatch.setattr(seeder, "lock_tenants", self.lock)
        monkeypatch.setattr(seeder, "_lock_seed", self.seed_lock)
        return self

    async def get_by_slug(self, slug):
        return self.rows.get(slug)

    async def create(self, *, obj_in):
        row = _tenant(**obj_in)
        self.rows[row.slug] = row
        self.created.append(row)
        return row

    async def update(self, *, db_obj, obj_in):
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def tiers_for(self, tenant_ids):
        return {row.id: row.tier for row in self.rows.values() if row.id in tenant_ids}

    async def get_default_end_user_org(self):
        return next((row for row in self.rows.values() if row.is_default_end_user_org and row.is_active), None)


def _db() -> Any:
    return Mock()


def _spy_writes(monkeypatch, store: _TenantStore):
    create, update = AsyncMock(side_effect=store.create), AsyncMock(side_effect=store.update)
    monkeypatch.setattr(seeder.tenant_dao, "create", create)
    monkeypatch.setattr(seeder.tenant_dao, "update", update)
    return create, update


@pytest.mark.asyncio
async def test_ensure_kabers_creates_when_missing(monkeypatch):
    store = _TenantStore().install(monkeypatch)
    create, _ = _spy_writes(monkeypatch, store)
    monkeypatch.setattr(seeder, "get_settings", Mock(side_effect=AssertionError("environment must not be consulted")))

    result = await seeder._ensure_kabers()

    assert (result.name, result.slug) == ("KABERs", "kabers")
    assert (result.is_system, result.is_default_end_user_org) == (True, True)
    create.assert_awaited_once()
    assert create.await_args is not None
    assert create.await_args.kwargs["obj_in"]["slug"] == "kabers"
    assert create.await_args.kwargs["obj_in"]["name"] == "KABERs"
    assert create.await_args.kwargs["obj_in"]["tier"] is OrganizationPlanId.PRO


@pytest.mark.asyncio
async def test_ensure_maraclaw_creates_with_the_ultra_tier(monkeypatch):
    store = _TenantStore().install(monkeypatch)
    create, _ = _spy_writes(monkeypatch, store)

    result = await seeder._ensure_maraclaw()

    assert (result.slug, result.tier) == ("maraclaw", OrganizationPlanId.ULTRA)
    assert create.await_args is not None
    assert create.await_args.kwargs["obj_in"]["tier"] is OrganizationPlanId.ULTRA


@pytest.mark.parametrize(("slug", "tier"), [("maraclaw", OrganizationPlanId.ULTRA), ("kabers", OrganizationPlanId.PRO)])
@pytest.mark.asyncio
async def test_current_tier_is_left_untouched(monkeypatch, slug, tier):
    tenant, db = _tenant(slug=slug, tier=tier), _db()
    store = _TenantStore(tenant).install(monkeypatch)
    _, update = _spy_writes(monkeypatch, store)

    result = await seeder._ensure_tier(db, tenant)

    assert result is tenant
    store.lock.assert_awaited_once_with(db, (tenant.id,))
    update.assert_not_awaited()


@pytest.mark.parametrize(
    ("slug", "tier", "current"),
    [
        ("kabers", OrganizationPlanId.PRO, OrganizationPlanId.MAX),
        ("kabers", OrganizationPlanId.PRO, OrganizationPlanId.ULTRA),
        ("maraclaw", OrganizationPlanId.ULTRA, OrganizationPlanId.PRO),
        ("maraclaw", OrganizationPlanId.ULTRA, OrganizationPlanId.MAX),
    ],
)
@pytest.mark.asyncio
async def test_changed_tier_is_reconciled_to_the_organization_tier_under_the_tenant_lock(
    monkeypatch, slug, tier, current
):
    tenant, db = _tenant(slug=slug, tier=current), _db()
    store = _TenantStore(tenant).install(monkeypatch)
    _, update = _spy_writes(monkeypatch, store)

    result = await seeder._ensure_tier(db, tenant)

    store.lock.assert_awaited_once_with(db, (tenant.id,))
    update.assert_awaited_once_with(db_obj=tenant, obj_in={"tier": tier})
    assert result.tier is tier


@pytest.mark.asyncio
async def test_ensure_kabers_does_not_steal_existing_default_flag(monkeypatch):
    kabers = _tenant(is_default_end_user_org=False)
    other = _tenant(slug="acme", name="Acme", is_default_end_user_org=True)

    async def get_by_slug(slug):
        return kabers if slug == "kabers" else None

    monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", get_by_slug)
    monkeypatch.setattr(seeder.tenant_dao, "get_default_end_user_org", AsyncMock(return_value=other))
    update = AsyncMock()
    monkeypatch.setattr(seeder.tenant_dao, "update", update)

    result = await seeder._ensure_kabers()
    assert result.id == kabers.id
    update.assert_not_awaited()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"is_system": False, "is_default_end_user_org": False}, "unrelated"),
        ({"is_active": False}, "disabled"),
    ],
)
@pytest.mark.asyncio
async def test_ensure_kabers_rejects_unusable_occupied_row_before_any_write(monkeypatch, overrides, message):
    store = _TenantStore(_tenant(**overrides)).install(monkeypatch)
    create, update = _spy_writes(monkeypatch, store)

    with pytest.raises(seeder.SystemOrgSeedError, match=message):
        await seeder._ensure_kabers()

    create.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_admin_seeds_validated_credentials_when_genesis_missing(monkeypatch):
    kabers = _tenant()
    monkeypatch.setattr(seeder, "get_settings", lambda: _settings(" Owner@Example.COM ", " spaced secret "))
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    attach = AsyncMock(return_value=SimpleNamespace(org_admin=SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(ATTACH, attach)

    await seeder._ensure_default_org_admin(kabers)

    attach.assert_awaited_once_with(
        tenant_id=kabers.id, admin_email="owner@example.com", admin_password=" spaced secret "
    )


@pytest.mark.parametrize("genesis_identity", [None, SimpleNamespace(email="", password_hash=None)])
@pytest.mark.asyncio
async def test_default_admin_leaves_established_genesis_unchanged_without_consulting_env(monkeypatch, genesis_identity):
    kabers = _tenant()
    genesis = SimpleNamespace(id=uuid.uuid4(), identity=genesis_identity)
    lookup = AsyncMock(return_value=genesis)
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", lookup)
    settings = Mock(side_effect=AssertionError("environment must not be consulted"))
    monkeypatch.setattr(seeder, "get_settings", settings)
    attach = AsyncMock()
    monkeypatch.setattr(ATTACH, attach)

    await seeder._ensure_default_org_admin(kabers)

    lookup.assert_awaited_once_with(kabers.id)
    settings.assert_not_called()
    attach.assert_not_awaited()


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("", ""),
        ("owner@example.com", ""),
        ("", "initial-secret"),
        ("not-an-email", "initial-secret"),
        ("owner@", "initial-secret"),
        ("owner@example.com", "12345"),
        ("owner@example.com", "x" * 129),
        ("owner@example.com", "      "),
    ],
)
@pytest.mark.asyncio
async def test_default_admin_rejects_missing_or_invalid_credentials(monkeypatch, email, password):
    monkeypatch.setattr(seeder, "get_settings", lambda: _settings(email, password))
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    attach = AsyncMock()
    monkeypatch.setattr(ATTACH, attach)

    with pytest.raises(seeder.SystemOrgSeedError) as exc:
        await seeder._ensure_default_org_admin(_tenant())

    attach.assert_not_awaited()
    if password.strip():
        assert password not in str(exc.value)


@pytest.mark.asyncio
async def test_default_admin_rejects_taken_email_without_leaking_password(monkeypatch):
    monkeypatch.setattr(seeder, "get_settings", lambda: _settings("taken@example.com", "initial-secret"))
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(ATTACH, AsyncMock(side_effect=AdminEmailTakenError("taken@example.com")))

    with pytest.raises(seeder.SystemOrgSeedError, match=r"taken@example\.com") as exc:
        await seeder._ensure_default_org_admin(_tenant())

    assert "initial-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_default_admin_concurrent_genesis_is_not_an_error(monkeypatch):
    monkeypatch.setattr(seeder, "get_settings", _valid_settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(ATTACH, AsyncMock(side_effect=GenesisOrgAdminExistsError))

    await seeder._ensure_default_org_admin(_tenant())


@pytest.mark.asyncio
async def test_ensure_system_orgs_commits_orgs_and_admin_in_one_transaction(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    store = _TenantStore().install(monkeypatch)
    monkeypatch.setattr(seeder, "get_settings", _valid_settings)
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    attach = AsyncMock(return_value=SimpleNamespace(org_admin=SimpleNamespace(id=uuid.uuid4())))
    monkeypatch.setattr(ATTACH, attach)
    _, update = _spy_writes(monkeypatch, store)

    maraclaw, kabers = await seeder.ensure_system_orgs()

    assert (maraclaw.slug, kabers.slug) == ("maraclaw", "kabers")
    assert kabers.is_default_end_user_org is True
    assert list(store.rows) == ["kabers", "maraclaw"]
    assert (maraclaw.tier, kabers.tier) == (OrganizationPlanId.ULTRA, OrganizationPlanId.PRO)
    assert attach.await_args is not None
    assert attach.await_args.kwargs["tenant_id"] == kabers.id
    assert update.await_args_list == [call(db_obj=kabers, obj_in={"is_default_end_user_org": True, "is_system": True})]
    assert (pool.raw.commits, pool.raw.rollbacks, pool.checkouts) == (1, 0, 1)
    store.seed_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_system_orgs_reconciles_a_changed_tier_of_an_existing_organization(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    maraclaw_row = _tenant(slug="maraclaw", name="MaraClaw", tier=OrganizationPlanId.ULTRA)
    kabers_row = _tenant(is_default_end_user_org=True, tier=OrganizationPlanId.MAX)
    store = _TenantStore(maraclaw_row, kabers_row).install(monkeypatch)
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=SimpleNamespace()))
    create, update = _spy_writes(monkeypatch, store)

    maraclaw, kabers = await seeder.ensure_system_orgs()

    create.assert_not_awaited()
    update.assert_awaited_once_with(db_obj=kabers_row, obj_in={"tier": OrganizationPlanId.PRO})
    assert (maraclaw.tier, kabers.tier) == (OrganizationPlanId.ULTRA, OrganizationPlanId.PRO)
    assert (pool.raw.commits, pool.raw.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_ensure_system_orgs_rolls_back_every_write_when_admin_seed_fails(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    _TenantStore().install(monkeypatch)
    monkeypatch.setattr(seeder, "get_settings", lambda: _settings("", ""))
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    attach = AsyncMock()
    monkeypatch.setattr(ATTACH, attach)

    with pytest.raises(seeder.SystemOrgSeedError, match="KABERS_ORG_ADMIN_EMAIL"):
        await seeder.ensure_system_orgs()

    attach.assert_not_awaited()
    assert (pool.raw.commits, pool.raw.rollbacks, pool.checkouts) == (0, 1, 1)
