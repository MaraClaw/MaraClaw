"""Fresh KABERs tier seeding against a live PostgreSQL schema (opt-in ``BOOTSTRAP_TEST_DATABASE_URL``)."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.dao.tenant_dao import tenant_dao
from app.records.organization_plan import OrganizationPlanId
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


@pytest.fixture(autouse=True)
def bootstrap_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(KABERS_ORG_ADMIN_EMAIL=EMAIL, KABERS_ORG_ADMIN_PASSWORD="initial-secret")
    monkeypatch.setattr(seeder, "get_settings", lambda: settings)


async def _admission_count(organization_scenario: OrganizationScenario) -> object:
    return await organization_scenario.db.fetchval("SELECT count(*) FROM organization_admission_requests")


async def test_fresh_kabers_resolves_pro_while_maraclaw_stays_untiered_across_reruns(
    organization_scenario: OrganizationScenario,
):
    maraclaw, kabers = await seeder.ensure_system_orgs()

    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO
    assert await tier_for_tenant(maraclaw.id) is None
    row = await organization_scenario.db.fetchone(
        "SELECT * FROM organization_admission_requests WHERE provisioned_tenant_id=%(tenant)s", {"tenant": kabers.id}
    )
    assert row is not None
    assert (row["status"], row["tier"], row["email"], row["organization_name"]) == ("approved", "pro", EMAIL, "KABERs")
    assert row["plan"] == plan_for(OrganizationPlanId.PRO).model_dump(mode="json")
    assert row["reviewed_at"] is not None
    assert row["provisioned_at"] is not None
    assert "bootstrap" in str(row["review_note"])
    assert (row["reviewed_by"], row["provisioned_admin_user_id"]) == (None, None)
    assert await organization_scenario.db.fetchval("SELECT count(*) FROM organization_admission_credential_emails") == 0

    _, again = await seeder.ensure_system_orgs()

    assert again.id == kabers.id
    assert await _admission_count(organization_scenario) == 1
    assert await tier_for_tenant(kabers.id) is OrganizationPlanId.PRO


@pytest.mark.parametrize("existing_tier", [OrganizationPlanId.MAX, None])
async def test_existing_kabers_keeps_its_tier_and_gains_no_admission(
    organization_scenario: OrganizationScenario, existing_tier: OrganizationPlanId | None
):
    tenant = await tenant_dao.create(
        obj_in={"name": "KABERs", "slug": "kabers", "im_provider": "web_only", "is_system": True, "is_active": True}
    )
    if existing_tier is not None:
        _ = await approve_admission_for(organization_scenario.db, tenant.id, existing_tier)

    _, kabers = await seeder.ensure_system_orgs()

    assert kabers.id == tenant.id
    assert await tier_for_tenant(tenant.id) is existing_tier
    assert await _admission_count(organization_scenario) == (0 if existing_tier is None else 1)


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
