"""System organization tier seeding against a live PostgreSQL schema (opt-in ``BOOTSTRAP_TEST_DATABASE_URL``)."""

import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.types import as_jsonb
from app.records.organization_admission import OrganizationAdmissionRecord
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.services import system_org_seeder as seeder, tenant_provisioning
from app.services.organization_plans import plan_for, tier_for_tenant
from organization_admission_fixtures import OrganizationScenario, organization_scenario
from tier_limit_fixtures import approve_admission_for

__all__ = ["organization_scenario"]

EMAIL = "kabers-owner@example.com"
LEFTOVERS = (
    "SELECT (SELECT count(*) FROM tenants WHERE slug IN ('kabers','maraclaw')) AS tenants, "
    "(SELECT count(*) FROM organization_admission_requests) AS admissions, "
    "(SELECT count(*) FROM identities WHERE email=%(email)s) AS identities"
)
ADMISSIONS = "SELECT *, xmin::text AS version FROM organization_admission_requests ORDER BY id"
OUTDATED_PRO_PLAN = {
    key: value
    for key, value in plan_for(OrganizationPlanId.PRO).model_dump(mode="json").items()
    if key != "daily_messages_per_agent"
} | {"monthly_price_usd": 99}


@pytest.fixture(autouse=True)
def bootstrap_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(KABERS_ORG_ADMIN_EMAIL=EMAIL, KABERS_ORG_ADMIN_PASSWORD="initial-secret")
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)


async def _admissions(db: DbConnection) -> list[dict[str, object]]:
    return [dict(row) for row in await db.fetchall(ADMISSIONS)]


async def _system_org(slug: str, name: str, *, default_end_user_org: bool = False) -> TenantRecord:
    return await tenant_dao.create(
        obj_in={
            "name": name,
            "slug": slug,
            "im_provider": "web_only",
            "is_system": True,
            "is_active": True,
            "is_default_end_user_org": default_end_user_org,
        }
    )


async def _make_outdated(db: DbConnection, request_id: UUID) -> None:
    await db.execute(
        "UPDATE organization_admission_requests SET plan=%(plan)s WHERE id=%(id)s",
        {"id": request_id, "plan": as_jsonb(OUTDATED_PRO_PLAN)},
    )


def _row_for(rows: list[dict[str, object]], tenant_id: UUID) -> dict[str, object]:
    (row,) = (row for row in rows if row["provisioned_tenant_id"] == tenant_id)
    return row


async def test_fresh_database_seeds_maraclaw_ultra_and_kabers_pro_and_a_rerun_writes_nothing(
    organization_scenario: OrganizationScenario,
):
    db = organization_scenario.db

    maraclaw, kabers = await seeder.ensure_system_orgs()

    assert await tier_for_tenant(maraclaw.id) is OrganizationPlanId.ULTRA
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    rows = await _admissions(db)
    assert len(rows) == 2
    for tenant, name, tier in (
        (maraclaw, "MaraClaw", OrganizationPlanId.ULTRA),
        (kabers, "KABERs", OrganizationPlanId.PRO),
    ):
        row = _row_for(rows, tenant.id)
        assert (row["status"], row["tier"], row["organization_name"]) == ("approved", tier.value, name)
        assert row["email"] == f"{tenant.slug}@system.invalid"
        assert row["plan"] == plan_for(tier).model_dump(mode="json")
        assert row["reviewed_at"] is not None
        assert row["provisioned_at"] is not None
        assert "bootstrap" in str(row["review_note"])
        assert (row["reviewed_by"], row["provisioned_admin_user_id"]) == (None, None)
        assert OrganizationAdmissionRecord.from_row(row).plan == plan_for(tier)
    assert await db.fetchval("SELECT count(*) FROM organization_admission_credential_emails") == 0

    again_maraclaw, again_kabers = await seeder.ensure_system_orgs()

    assert (again_maraclaw.id, again_kabers.id) == (maraclaw.id, kabers.id)
    assert await _admissions(db) == rows


async def test_existing_database_refreshes_the_outdated_kabers_row_and_adds_the_missing_maraclaw_row(
    organization_scenario: OrganizationScenario,
):
    # Given KABERs linked to a Pro row holding an outdated plan snapshot, and MaraClaw with no row at all.
    db = organization_scenario.db
    kabers = await _system_org(seeder.KABERS_SLUG, seeder.KABERS_NAME, default_end_user_org=True)
    maraclaw = await _system_org(seeder.MARACLAW_SLUG, "MaraClaw")
    old_id = await approve_admission_for(db, kabers.id, OrganizationPlanId.PRO)
    await _make_outdated(db, old_id)
    before = _row_for(await _admissions(db), kabers.id)
    with pytest.raises(ValidationError):
        _ = OrganizationAdmissionRecord.from_row(before)

    # When the system organizations are ensured.
    await seeder.ensure_system_orgs()

    # Then the KABERs row is refreshed in place, MaraClaw gets its Ultra row, and both rows validate.
    rows = await _admissions(db)
    assert len(rows) == 2
    refreshed = _row_for(rows, kabers.id)
    assert refreshed["id"] == old_id
    assert refreshed["plan"] == plan_for(OrganizationPlanId.PRO).model_dump(mode="json")
    assert {key: value for key, value in refreshed.items() if key not in ("plan", "version")} == {
        key: value for key, value in before.items() if key not in ("plan", "version")
    }
    assert OrganizationAdmissionRecord.from_row(refreshed).tier is OrganizationPlanId.PRO
    inserted = _row_for(rows, maraclaw.id)
    assert (inserted["status"], inserted["tier"], inserted["email"]) == ("approved", "ultra", "maraclaw@system.invalid")
    assert OrganizationAdmissionRecord.from_row(inserted).plan == plan_for(OrganizationPlanId.ULTRA)
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    assert await tier_for_tenant(maraclaw.id) is OrganizationPlanId.ULTRA

    # And a rerun writes nothing: same rows, same row versions.
    await seeder.ensure_system_orgs()

    assert await _admissions(db) == rows


@pytest.mark.parametrize("existing_tier", [OrganizationPlanId.MAX, OrganizationPlanId.ULTRA, None])
async def test_existing_kabers_is_reconciled_to_pro_with_a_single_row(
    organization_scenario: OrganizationScenario, existing_tier: OrganizationPlanId | None
):
    db = organization_scenario.db
    tenant = await _system_org(seeder.KABERS_SLUG, seeder.KABERS_NAME)
    existing_id = None if existing_tier is None else await approve_admission_for(db, tenant.id, existing_tier)

    _, kabers = await seeder.ensure_system_orgs()

    assert kabers.id == tenant.id
    assert await tier_for_tenant(tenant.id) is OrganizationPlanId.PRO
    row = _row_for(await _admissions(db), tenant.id)
    assert row["plan"] == plan_for(OrganizationPlanId.PRO).model_dump(mode="json")
    if existing_id is not None:
        assert row["id"] == existing_id


@pytest.mark.parametrize("doubled", [seeder.MARACLAW_SLUG, seeder.KABERS_SLUG])
async def test_two_linked_admissions_abort_the_bootstrap_and_commit_nothing(
    organization_scenario: OrganizationScenario, doubled: str
):
    db = organization_scenario.db
    tenant = await _system_org(doubled, "MaraClaw" if doubled == seeder.MARACLAW_SLUG else seeder.KABERS_NAME)
    for tier in (OrganizationPlanId.PRO, OrganizationPlanId.MAX):
        _ = await approve_admission_for(db, tenant.id, tier)
    before = await _admissions(db)

    with pytest.raises(seeder.SystemOrgSeedError, match=doubled):
        await seeder.ensure_system_orgs()

    assert await _admissions(db) == before
    assert await db.fetchone(LEFTOVERS, {"email": EMAIL}) == {"tenants": 1, "admissions": 2, "identities": 0}


async def test_concurrent_bootstraps_leave_one_tier_row_per_organization(organization_scenario: OrganizationScenario):
    db = organization_scenario.db
    kabers = await _system_org(seeder.KABERS_SLUG, seeder.KABERS_NAME, default_end_user_org=True)
    maraclaw = await _system_org(seeder.MARACLAW_SLUG, "MaraClaw")
    _ = await user_dao.create(
        obj_in={"tenant_id": kabers.id, "display_name": "Genesis", "role": "org_admin", "is_genesis": True}
    )

    _ = await asyncio.gather(*(seeder.ensure_system_orgs() for _ in range(4)))

    rows = await _admissions(db)
    assert len(rows) == 2
    assert {row["provisioned_tenant_id"] for row in rows} == {kabers.id, maraclaw.id}
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    assert await tier_for_tenant(maraclaw.id) is OrganizationPlanId.ULTRA


async def test_failure_after_admin_creation_rolls_back_organization_admission_and_admin(
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
    assert leftovers == {"tenants": 0, "admissions": 0, "identities": 0}
