"""System organization seeder."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from tests.transaction_pool_fakes import CountingPool

from app.db import session
from app.records.organization_admission import LinkedAdmissionPlan
from app.records.organization_plan import OrganizationPlanId
from app.services import system_org_seeder as seeder
from app.services.organization_plans import plan_for
from app.services.tenant_provisioning import AdminEmailTakenError, GenesisOrgAdminExistsError

ATTACH = "app.services.tenant_provisioning.attach_genesis_org_admin"
OUTDATED_PRO_PLAN = {
    key: value
    for key, value in plan_for(OrganizationPlanId.PRO).model_dump(mode="json").items()
    if key != "daily_messages_per_agent"
} | {"monthly_price_usd": 99}


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


def _linked(tier: OrganizationPlanId, plan: object = None) -> LinkedAdmissionPlan:
    stored = plan_for(tier).model_dump(mode="json") if plan is None else plan
    return LinkedAdmissionPlan(id=uuid.uuid4(), tier=tier, plan=stored)


def _db() -> Any:
    return Mock()


class _Admissions:
    """Approved admission rows keyed by tenant id, mirroring the DAO and lock calls the seeder makes."""

    def __init__(self, monkeypatch, rows: dict[uuid.UUID, list[LinkedAdmissionPlan]] | None = None):
        self.rows = rows or {}
        self.read = AsyncMock(side_effect=self._read)
        self.create, self.refresh, self.lock = AsyncMock(), AsyncMock(), AsyncMock()
        monkeypatch.setattr(seeder.organization_admission_dao, "approved_plans_for_tenant", self.read)
        monkeypatch.setattr(seeder.organization_admission_dao, "create", self.create)
        monkeypatch.setattr(seeder.organization_admission_dao, "refresh_tier_and_plan", self.refresh)
        monkeypatch.setattr(seeder, "lock_tenants", self.lock)

    async def _read(self, tenant_id: uuid.UUID) -> list[LinkedAdmissionPlan]:
        return self.rows.get(tenant_id, [])

    def assert_no_writes(self) -> None:
        self.create.assert_not_awaited()
        self.refresh.assert_not_awaited()


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
    admissions = _Admissions(monkeypatch)

    result = await seeder._ensure_kabers()

    assert (result.name, result.slug) == ("KABERs", "kabers")
    assert (result.is_system, result.is_default_end_user_org) == (True, True)
    create.assert_awaited_once()
    assert create.await_args is not None
    assert create.await_args.kwargs["obj_in"]["slug"] == "kabers"
    assert create.await_args.kwargs["obj_in"]["name"] == "KABERs"
    admissions.assert_no_writes()


@pytest.mark.asyncio
async def test_ensure_kabers_leaves_existing_organization_without_a_new_admission(monkeypatch):
    _TenantStore(_tenant(is_default_end_user_org=True)).install(monkeypatch)
    monkeypatch.setattr(seeder, "get_settings", Mock(side_effect=AssertionError("environment must not be consulted")))
    admissions = _Admissions(monkeypatch)

    await seeder._ensure_kabers()

    admissions.assert_no_writes()


@pytest.mark.parametrize(
    ("slug", "name", "tier"),
    [("maraclaw", "MaraClaw", OrganizationPlanId.ULTRA), ("kabers", "KABERs", OrganizationPlanId.PRO)],
)
@pytest.mark.asyncio
async def test_missing_tier_row_is_inserted_with_the_organization_tier_and_the_current_plan(
    monkeypatch, slug, name, tier
):
    tenant, db = _tenant(slug=slug, name=name), _db()
    admissions = _Admissions(monkeypatch)
    monkeypatch.setattr(seeder, "get_settings", Mock(side_effect=AssertionError("environment must not be consulted")))

    await seeder._ensure_tier(db, tenant)

    admissions.lock.assert_awaited_once_with(db, (tenant.id,))
    admissions.refresh.assert_not_awaited()
    admissions.create.assert_awaited_once()
    assert admissions.create.await_args is not None
    fields = admissions.create.await_args.kwargs["obj_in"]
    assert fields["provisioned_tenant_id"] == tenant.id
    assert (fields["status"], fields["tier"], fields["plan"]) == (
        "approved",
        tier.value,
        plan_for(tier).model_dump(mode="json"),
    )
    assert (fields["email"], fields["organization_name"]) == (f"{slug}@system.invalid", name)
    assert fields["reviewed_at"] is not None
    assert fields["reviewed_at"] == fields["provisioned_at"]
    assert "bootstrap" in fields["review_note"]
    assert {"reviewed_by", "provisioned_admin_user_id"}.isdisjoint(fields)


@pytest.mark.parametrize(("slug", "tier"), [("maraclaw", OrganizationPlanId.ULTRA), ("kabers", OrganizationPlanId.PRO)])
@pytest.mark.asyncio
async def test_current_tier_row_is_left_untouched(monkeypatch, slug, tier):
    tenant = _tenant(slug=slug)
    admissions = _Admissions(monkeypatch, {tenant.id: [_linked(tier)]})

    await seeder._ensure_tier(_db(), tenant)

    admissions.assert_no_writes()


@pytest.mark.parametrize(
    ("slug", "tier", "stored"),
    [
        ("kabers", OrganizationPlanId.PRO, _linked(OrganizationPlanId.PRO, OUTDATED_PRO_PLAN)),
        ("kabers", OrganizationPlanId.PRO, _linked(OrganizationPlanId.MAX)),
        ("maraclaw", OrganizationPlanId.ULTRA, _linked(OrganizationPlanId.PRO)),
        ("maraclaw", OrganizationPlanId.ULTRA, _linked(OrganizationPlanId.ULTRA, {})),
    ],
    ids=["outdated-snapshot", "other-tier-kabers", "other-tier-maraclaw", "empty-snapshot"],
)
@pytest.mark.asyncio
async def test_stale_tier_row_is_refreshed_in_place(monkeypatch, slug, tier, stored):
    tenant = _tenant(slug=slug)
    admissions = _Admissions(monkeypatch, {tenant.id: [stored]})

    await seeder._ensure_tier(_db(), tenant)

    admissions.create.assert_not_awaited()
    admissions.refresh.assert_awaited_once_with(stored.id, tier=tier, plan=plan_for(tier))


@pytest.mark.parametrize("slug", ["maraclaw", "kabers"])
@pytest.mark.asyncio
async def test_two_linked_admissions_abort_the_seed_without_writing(monkeypatch, slug):
    tenant = _tenant(slug=slug)
    both = [_linked(OrganizationPlanId.PRO), _linked(OrganizationPlanId.PRO)]
    admissions = _Admissions(monkeypatch, {tenant.id: both})

    with pytest.raises(seeder.SystemOrgSeedError, match=slug):
        await seeder._ensure_tier(_db(), tenant)

    admissions.assert_no_writes()


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
    admissions = _Admissions(monkeypatch)

    maraclaw, kabers = await seeder.ensure_system_orgs()

    assert (maraclaw.slug, kabers.slug) == ("maraclaw", "kabers")
    assert kabers.is_default_end_user_org is True
    assert list(store.rows) == ["kabers", "maraclaw"]
    assert attach.await_args is not None
    assert attach.await_args.kwargs["tenant_id"] == kabers.id
    assert admissions.create.await_count == 2
    inserted = {
        call.kwargs["obj_in"]["provisioned_tenant_id"]: call.kwargs["obj_in"]["tier"]
        for call in admissions.create.await_args_list
    }
    assert inserted == {maraclaw.id: "ultra", kabers.id: "pro"}
    assert (pool.raw.commits, pool.raw.rollbacks, pool.checkouts) == (1, 0, 1)


@pytest.mark.asyncio
async def test_ensure_system_orgs_reconciles_existing_organizations_without_a_second_row(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    maraclaw_row = _tenant(slug="maraclaw", name="MaraClaw")
    kabers_row = _tenant(is_default_end_user_org=True)
    _TenantStore(maraclaw_row, kabers_row).install(monkeypatch)
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=SimpleNamespace()))
    outdated = _linked(OrganizationPlanId.PRO, OUTDATED_PRO_PLAN)
    admissions = _Admissions(monkeypatch, {kabers_row.id: [outdated]})

    await seeder.ensure_system_orgs()

    admissions.refresh.assert_awaited_once_with(
        outdated.id, tier=OrganizationPlanId.PRO, plan=plan_for(OrganizationPlanId.PRO)
    )
    admissions.create.assert_awaited_once()
    assert admissions.create.await_args is not None
    assert admissions.create.await_args.kwargs["obj_in"]["provisioned_tenant_id"] == maraclaw_row.id
    assert (pool.raw.commits, pool.raw.rollbacks) == (1, 0)


@pytest.mark.asyncio
async def test_ensure_system_orgs_rolls_back_every_write_when_a_system_org_is_linked_twice(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    maraclaw_row = _tenant(slug="maraclaw", name="MaraClaw")
    _TenantStore(maraclaw_row).install(monkeypatch)
    _Admissions(monkeypatch, {maraclaw_row.id: [_linked(OrganizationPlanId.ULTRA), _linked(OrganizationPlanId.ULTRA)]})
    attach = AsyncMock()
    monkeypatch.setattr(ATTACH, attach)

    with pytest.raises(seeder.SystemOrgSeedError, match="maraclaw"):
        await seeder.ensure_system_orgs()

    attach.assert_not_awaited()
    assert (pool.raw.commits, pool.raw.rollbacks, pool.checkouts) == (0, 1, 1)


@pytest.mark.asyncio
async def test_ensure_system_orgs_rolls_back_every_write_when_admin_seed_fails(monkeypatch):
    pool = CountingPool()
    monkeypatch.setattr(session, "get_pool", lambda: pool)
    _TenantStore().install(monkeypatch)
    _Admissions(monkeypatch)
    monkeypatch.setattr(seeder, "get_settings", lambda: _settings("", ""))
    monkeypatch.setattr(seeder.user_dao, "genesis_org_admin_for_tenant", AsyncMock(return_value=None))
    attach = AsyncMock()
    monkeypatch.setattr(ATTACH, attach)

    with pytest.raises(seeder.SystemOrgSeedError, match="KABERS_ORG_ADMIN_EMAIL"):
        await seeder.ensure_system_orgs()

    attach.assert_not_awaited()
    assert (pool.raw.commits, pool.raw.rollbacks, pool.checkouts) == (0, 1, 1)
