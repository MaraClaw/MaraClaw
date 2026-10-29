"""System organization seeder."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from tests.transaction_pool_fakes import CountingPool

from app.db import session
from app.services import system_org_seeder as seeder
from app.services.tenant_provisioning import AdminEmailTakenError, GenesisOrgAdminExistsError

ATTACH = "app.services.tenant_provisioning.attach_genesis_org_admin"


def _tenant(**overrides: Any) -> Any:
    values = {
        "id": uuid.uuid4(),
        "name": "KABERs",
        "slug": "kabers",
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

    def install(self, monkeypatch):
        monkeypatch.setattr(seeder.tenant_dao, "get_by_slug", self.get_by_slug)
        monkeypatch.setattr(seeder.tenant_dao, "create", self.create)
        monkeypatch.setattr(seeder.tenant_dao, "update", self.update)
        monkeypatch.setattr(seeder.tenant_dao, "get_default_end_user_org", self.get_default_end_user_org)
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

    async def get_default_end_user_org(self):
        return next((row for row in self.rows.values() if row.is_default_end_user_org and row.is_active), None)


def _spy_writes(monkeypatch, store: _TenantStore):
    create, update = AsyncMock(side_effect=store.create), AsyncMock(side_effect=store.update)
    monkeypatch.setattr(seeder.tenant_dao, "create", create)
    monkeypatch.setattr(seeder.tenant_dao, "update", update)
    return create, update


@pytest.mark.asyncio
async def test_ensure_kabers_creates_when_missing(monkeypatch):
    store = _TenantStore().install(monkeypatch)
    create, _ = _spy_writes(monkeypatch, store)

    result = await seeder._ensure_kabers()

    assert (result.name, result.slug) == ("KABERs", "kabers")
    assert (result.is_system, result.is_default_end_user_org) == (True, True)
    create.assert_awaited_once()
    assert create.await_args is not None
    assert create.await_args.kwargs["obj_in"]["slug"] == "kabers"
    assert create.await_args.kwargs["obj_in"]["name"] == "KABERs"


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

    maraclaw, kabers = await seeder.ensure_system_orgs()

    assert (maraclaw.slug, kabers.slug) == ("maraclaw", "kabers")
    assert kabers.is_default_end_user_org is True
    assert list(store.rows) == ["kabers", "maraclaw"]
    assert attach.await_args is not None
    assert attach.await_args.kwargs["tenant_id"] == kabers.id
    assert (pool.raw.commits, pool.raw.rollbacks, pool.checkouts) == (1, 0, 1)


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
