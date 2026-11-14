"""System organization tier seeding against a live PostgreSQL schema (opt-in ``BOOTSTRAP_TEST_DATABASE_URL``)."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.services import system_org_seeder as seeder, tenant_provisioning
from app.services.organization_plans import tier_for_tenant
from organization_admission_fixtures import OrganizationScenario, organization_scenario

__all__ = ["organization_scenario"]

EMAIL = "kabers-owner@example.com"
LEFTOVERS = (
    "SELECT (SELECT count(*) FROM tenants WHERE slug IN ('kabers','maraclaw')) AS tenants, "
    "(SELECT count(*) FROM identities WHERE email=%(email)s) AS identities"
)
SYSTEM_TENANTS = "SELECT slug, tier, phone, xmin::text AS version FROM tenants WHERE is_system ORDER BY slug"


@pytest.fixture(autouse=True)
def bootstrap_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(KABERS_ORG_ADMIN_EMAIL=EMAIL, KABERS_ORG_ADMIN_PASSWORD="initial-secret")
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)


async def _system_tenants(db: DbConnection) -> list[dict[str, object]]:
    return [dict(row) for row in await db.fetchall(SYSTEM_TENANTS)]


async def _system_org(
    slug: str, name: str, tier: OrganizationPlanId, *, default_end_user_org: bool = False
) -> TenantRecord:
    return await tenant_dao.create(
        obj_in={
            "name": name,
            "slug": slug,
            "im_provider": "web_only",
            "is_system": True,
            "is_active": True,
            "is_default_end_user_org": default_end_user_org,
            "tier": tier,
        }
    )


async def test_fresh_database_seeds_maraclaw_ultra_and_kabers_pro_and_a_rerun_writes_nothing(
    organization_scenario: OrganizationScenario,
):
    db = organization_scenario.db
    admissions_before = await db.fetchval("SELECT count(*) FROM organization_admission_requests")

    maraclaw, kabers = await seeder.ensure_system_orgs()

    assert (maraclaw.tier, kabers.tier) == (OrganizationPlanId.ULTRA, OrganizationPlanId.PRO)
    assert await tier_for_tenant(maraclaw.id) is OrganizationPlanId.ULTRA
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    rows = await _system_tenants(db)
    assert [(row["slug"], row["tier"]) for row in rows] == [("kabers", "pro"), ("maraclaw", "ultra")]
    assert [row["phone"] for row in rows] == [None, None]
    assert await db.fetchval("SELECT count(*) FROM organization_admission_requests") == admissions_before

    again_maraclaw, again_kabers = await seeder.ensure_system_orgs()

    assert (again_maraclaw.id, again_kabers.id) == (maraclaw.id, kabers.id)
    assert await _system_tenants(db) == rows


@pytest.mark.parametrize(
    ("kabers_tier", "maraclaw_tier"),
    [
        (OrganizationPlanId.MAX, OrganizationPlanId.ULTRA),
        (OrganizationPlanId.ULTRA, OrganizationPlanId.PRO),
        (OrganizationPlanId.PRO, OrganizationPlanId.MAX),
    ],
)
async def test_existing_organizations_are_set_back_to_their_tiers_and_a_rerun_writes_nothing(
    organization_scenario: OrganizationScenario, kabers_tier: OrganizationPlanId, maraclaw_tier: OrganizationPlanId
):
    # Given system organizations whose stored tiers differ from SYSTEM_ORG_TIERS.
    db = organization_scenario.db
    kabers = await _system_org(seeder.KABERS_SLUG, seeder.KABERS_NAME, kabers_tier, default_end_user_org=True)
    maraclaw = await _system_org(seeder.MARACLAW_SLUG, "MaraClaw", maraclaw_tier)

    # When the system organizations are ensured.
    returned_maraclaw, returned_kabers = await seeder.ensure_system_orgs()

    # Then each row and each returned record carries its own tier, and the same rows are kept.
    assert (returned_maraclaw.id, returned_kabers.id) == (maraclaw.id, kabers.id)
    assert (returned_maraclaw.tier, returned_kabers.tier) == (OrganizationPlanId.ULTRA, OrganizationPlanId.PRO)
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    assert await tier_for_tenant(maraclaw.id) is OrganizationPlanId.ULTRA
    reconciled = await _system_tenants(db)

    # And a rerun writes nothing: same row versions.
    await seeder.ensure_system_orgs()

    assert await _system_tenants(db) == reconciled


async def test_concurrent_bootstraps_leave_each_organization_on_its_tier(organization_scenario: OrganizationScenario):
    db = organization_scenario.db
    kabers = await _system_org(
        seeder.KABERS_SLUG, seeder.KABERS_NAME, OrganizationPlanId.MAX, default_end_user_org=True
    )
    maraclaw = await _system_org(seeder.MARACLAW_SLUG, "MaraClaw", OrganizationPlanId.PRO)
    _ = await user_dao.create(
        obj_in={"tenant_id": kabers.id, "display_name": "Genesis", "role": "org_admin", "is_genesis": True}
    )

    _ = await asyncio.gather(*(seeder.ensure_system_orgs() for _ in range(4)))

    assert len(await _system_tenants(db)) == 2
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    assert await tier_for_tenant(maraclaw.id) is OrganizationPlanId.ULTRA


async def test_failure_after_admin_creation_rolls_back_organizations_and_admin(
    organization_scenario: OrganizationScenario, monkeypatch: pytest.MonkeyPatch
):
    attach = tenant_provisioning.attach_genesis_org_admin

    async def attach_then_fail(*, tenant_id: UUID, admin_email: str, admin_password: str) -> object:
        _ = await attach(tenant_id=tenant_id, admin_email=admin_email, admin_password=admin_password)
        raise RuntimeError("after admin")

    monkeypatch.setattr(tenant_provisioning, "attach_genesis_org_admin", attach_then_fail)

    with pytest.raises(RuntimeError, match="after admin"):
        await seeder.ensure_system_orgs()

    leftovers = await organization_scenario.db.fetchone(LEFTOVERS, {"email": EMAIL})
    assert leftovers == {"tenants": 0, "identities": 0}
