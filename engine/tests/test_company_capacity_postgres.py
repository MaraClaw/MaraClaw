"""Company-wide account and agent caps against a real schema, locks and the real HTTP stack."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app import main
from app.api import auth, users
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.session import bind_crud_connection
from app.records.organization_plan import OrganizationPlanId
from app.records.tenant import TenantRecord
from app.records.user import UserRecord
from app.schemas.schemas import UserLogin, VerifyEmailRequest
from app.services import (
    admin_provisioning,
    org_membership,
    registration_service as registration,
    tenant_lifecycle,
    tenant_provisioning,
)
from app.services.auth_audience import AuthAudience
from app.services.company_capacity import CompanyAccountLimitReachedError, company_usage
from app.services.email_verification_service import email_verification_service
from capacity_fixtures import grant_extras
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario"]

# The scenario company has seven accounts (genesis, oa2, oa3, owner, other, target, helper) and one platform
# administrator who must not count; Pro includes five accounts, so extras of N give a cap of 5 + N.
ACCOUNTS = 7


def _agent_message(limit: int) -> str:
    return f"Your company has used all {limit} agents. An org admin can buy extra agents on the Users page in the admin console."


@pytest.fixture(autouse=True)
def _no_okr_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registration.registration_service, "_run_okr_member_hook", AsyncMock())


async def _company(scenario: DeletionScenario) -> TenantRecord:
    tenant = await tenant_dao.get(scenario.tenant_id)
    assert tenant is not None
    return tenant


async def _newcomer(name: str) -> UserRecord:
    """An active member who belongs to no company yet."""
    identity = await identity_dao.create(
        obj_in={"email": f"{name}@new.test", "email_verified": True, "password_hash": "x", "is_platform_admin": False}
    )
    return await user_dao.create(
        obj_in={
            "identity_id": identity.id,
            "tenant_id": None,
            "display_name": name,
            "role": "member",
            "is_active": True,
        }
    )


async def _fresh_user(user_id: UUID) -> UserRecord:
    user = await user_dao.get_with_identity(user_id, fresh=True)
    assert user is not None
    return user


async def _fresh(scenario: DeletionScenario, who: str) -> UserRecord:
    return await _fresh_user(scenario[who].id)


async def _accounts(scenario: DeletionScenario) -> int:
    return (await company_usage(scenario.db, scenario.tenant_id))[0]


async def _join(scenario: DeletionScenario, newcomer: UserRecord | str) -> UserRecord:
    user = newcomer if isinstance(newcomer, UserRecord) else await _newcomer(newcomer)
    return await org_membership.attach_user_to_org(user, await _company(scenario))


async def _set_active(scenario: DeletionScenario, who: str, active: bool) -> UserRecord:
    return await admin_provisioning.set_end_user_active(
        actor=await _fresh(scenario, "genesis"), target=await _fresh(scenario, who), is_active=active
    )


# ── accounts ────────────────────────────────────────────────────────────


async def test_usage_counts_active_accounts_of_the_company_only(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    assert await company_usage(scenario.db, scenario.tenant_id) == (ACCOUNTS, 0)
    await scenario.set_user("other", is_active=False)
    _ = await scenario.agent("owner", "Counted")
    _ = await scenario.agent("owner", "Expired", is_expired=True)
    _ = await scenario.agent("owner", "System", is_system=True)
    _ = await scenario.agent("owner", "Foreign", tenant_id=scenario.foreign_tenant_id)
    assert await company_usage(scenario.db, scenario.tenant_id) == (ACCOUNTS - 1, 1)
    assert await company_usage(scenario.db, scenario.foreign_tenant_id) == (1, 1)


async def test_the_account_after_the_last_free_one_is_refused_and_extras_raise_the_cap(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=2)  # cap 7 == the seven accounts already there

    late = await _newcomer("late")
    with pytest.raises(CompanyAccountLimitReachedError) as refused:
        _ = await _join(scenario, late)
    assert (refused.value.limit, refused.value.used) == (ACCOUNTS, ACCOUNTS)
    assert await _accounts(scenario) == ACCOUNTS
    assert (await _fresh_user(late.id)).tenant_id is None

    grant_extras(monkeypatch, accounts=3)
    joined = await _join(scenario, late)
    assert joined.tenant_id == scenario.tenant_id
    assert await _accounts(scenario) == ACCOUNTS + 1
    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await _join(scenario, "later")


async def test_concurrent_joins_cannot_both_take_the_last_account(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=3)  # cap 8, one free
    racers = [await _newcomer(f"racer{n}") for n in range(4)]
    company = await _company(scenario)

    results = await asyncio.gather(
        *(org_membership.attach_user_to_org(racer, company) for racer in racers), return_exceptions=True
    )

    assert sorted(type(result).__name__ for result in results) == [
        "CompanyAccountLimitReachedError",
        "CompanyAccountLimitReachedError",
        "CompanyAccountLimitReachedError",
        "UserRecord",
    ]
    assert await _accounts(scenario) == ACCOUNTS + 1


@pytest.mark.parametrize("uncapped", ["untiered", "system"])
async def test_untiered_and_system_organizations_take_accounts_past_the_included_allocation(
    deletion_scenario: DeletionScenario, uncapped: str
):
    scenario = deletion_scenario
    if uncapped == "system":
        _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
        await scenario.db.execute("UPDATE tenants SET is_system = true WHERE id = %(id)s", {"id": scenario.tenant_id})
    for n in range(3):
        joined = await _join(scenario, f"extra{n}")
        assert joined.tenant_id == scenario.tenant_id
    assert await _accounts(scenario) == ACCOUNTS + 3  # Pro includes 5; no cap applies


async def test_reactivating_a_member_takes_an_account_and_the_member_is_not_counted_against_itself(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=2)  # cap 7
    _ = await _set_active(scenario, "target", False)
    _ = await _join(scenario, "taker")
    assert await _accounts(scenario) == ACCOUNTS

    with pytest.raises(CompanyAccountLimitReachedError) as refused:
        _ = await _set_active(scenario, "target", True)
    assert (refused.value.limit, refused.value.used) == (ACCOUNTS, ACCOUNTS)
    assert not (await _fresh(scenario, "target")).is_active

    _ = await _set_active(scenario, "other", False)
    reactivated = await _set_active(scenario, "target", True)
    assert reactivated.is_active
    assert await _accounts(scenario) == ACCOUNTS


async def test_creating_or_reactivating_an_org_admin_takes_an_account(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=2)  # cap 7 == used

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await admin_provisioning.create_additional_org_admin(
            tenant_id=scenario.tenant_id, admin_email="oa4@example.test", admin_password="temp-password"
        )
    assert await identity_dao.get_by_email("oa4@example.test") is None

    genesis = await _fresh(scenario, "genesis")
    _ = await admin_provisioning.set_peer_admin_active(
        actor=genesis, target=await _fresh(scenario, "oa3"), is_active=False
    )
    _ = await _join(scenario, "taker")
    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await admin_provisioning.set_peer_admin_active(
            actor=genesis, target=await _fresh(scenario, "oa3"), is_active=True
        )

    grant_extras(monkeypatch, accounts=4)
    created = await admin_provisioning.create_additional_org_admin(
        tenant_id=scenario.tenant_id, admin_email="oa4@example.test", admin_password="temp-password"
    )
    assert created.user.tenant_id == scenario.tenant_id
    assert await _accounts(scenario) == ACCOUNTS + 1  # oa3 stays inactive, taker and oa4 joined


async def test_registration_into_a_full_company_creates_no_user(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=2)
    identity = await identity_dao.create(
        obj_in={"email": "reg@new.test", "email_verified": True, "password_hash": "x", "is_platform_admin": False}
    )

    with pytest.raises(CompanyAccountLimitReachedError):
        _ = await registration.registration_service.create_user_with_identity(
            identity=identity, tenant_id=scenario.tenant_id
        )

    assert await user_dao.get_by_identity_id(identity.id) == []
    assert await _accounts(scenario) == ACCOUNTS


@pytest.fixture
async def http(deletion_scenario: DeletionScenario) -> AsyncIterator[AsyncClient]:
    """The real users router with the application's handler for the refusal."""
    app = FastAPI()
    app.include_router(users.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])

    @app.exception_handler(CompanyAccountLimitReachedError)
    async def refused(request: Request, exc: CompanyAccountLimitReachedError):
        return await main.company_account_limit(request, exc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://capacity.test") as client:
        yield client


async def test_activating_a_member_of_a_full_company_answers_409_with_the_limit(
    deletion_scenario: DeletionScenario, http: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=2)
    _ = await _set_active(scenario, "target", False)
    _ = await _join(scenario, "taker")

    response = await http.patch(
        f"/api/users/{scenario['target'].id}/active", json={"is_active": True}, headers=scenario.headers("genesis")
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "company_account_limit_reached",
        "message": (
            f"Your company has used all {ACCOUNTS} accounts. "
            "An org admin can buy extra accounts on the Users page in the admin console."
        ),
        "limit": ACCOUNTS,
        "used": ACCOUNTS,
    }
    assert not (await _fresh(scenario, "target")).is_active


# ── email verification, company re-enable and genesis repair ────────────


async def _deactivated_unverified_target(
    scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch, mode: str
) -> UUID:
    """Deactivate the target (``ACCOUNTS - 1`` remain) and clear their email verification; return their identity.

    ``full`` fills the Pro cap of seven again with a newcomer, ``room`` leaves a free slot, ``untiered`` and
    ``system`` have no cap.
    """
    if mode != "untiered":
        _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    if mode == "system":
        await scenario.db.execute("UPDATE tenants SET is_system = true WHERE id = %(id)s", {"id": scenario.tenant_id})
    grant_extras(monkeypatch, accounts=2 if mode == "full" else 3)
    identity_id = scenario["target"].identity_id
    assert identity_id is not None
    await scenario.db.execute("UPDATE identities SET email_verified = false WHERE id = %(id)s", {"id": identity_id})
    _ = await _set_active(scenario, "target", False)
    if mode == "full":
        _ = await _join(scenario, "taker")
    return identity_id


async def _email_verified(scenario: DeletionScenario, identity_id: UUID) -> bool:
    return bool(
        await scenario.db.fetchval("SELECT email_verified FROM identities WHERE id = %(id)s", {"id": identity_id})
    )


async def _click_verification_link(identity_id: UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    consume = AsyncMock(return_value={"identity_id": identity_id})
    monkeypatch.setattr(email_verification_service, "consume_email_verification_token", consume)
    monkeypatch.setattr("app.services.domain_admission_auto.stage_verified_domain_admission", AsyncMock())
    _ = await auth.verify_email_flow(VerifyEmailRequest(token="verify-token"), AuthAudience.SHARED)


async def _log_in_without_smtp(identity_id: UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.system_email_service.resolve_email_config_async", AsyncMock(return_value=None))
    identity = await identity_dao.get(identity_id)
    assert identity is not None
    login = UserLogin.model_construct(login_identifier=identity.email, password="qa-password", tenant_id=None)
    _ = await auth.login_flow(login, BackgroundTasks(), Request({"type": "http", "headers": []}), AuthAudience.SHARED)


async def test_the_verification_link_leaves_a_deactivated_member_inactive_in_a_full_company(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    identity_id = await _deactivated_unverified_target(scenario, monkeypatch, "full")

    await _click_verification_link(identity_id, monkeypatch)

    assert await _email_verified(scenario, identity_id)
    assert not (await _fresh(scenario, "target")).is_active
    assert await _accounts(scenario) == ACCOUNTS


async def test_login_without_smtp_verifies_the_identity_but_leaves_a_deactivated_member_inactive_in_a_full_company(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    identity_id = await _deactivated_unverified_target(scenario, monkeypatch, "full")

    with pytest.raises(HTTPException) as refused:
        await _log_in_without_smtp(identity_id, monkeypatch)

    assert (refused.value.status_code, refused.value.detail) == (403, "Your account has been disabled.")
    assert await _email_verified(scenario, identity_id)
    assert not (await _fresh(scenario, "target")).is_active
    assert await _accounts(scenario) == ACCOUNTS


@pytest.mark.parametrize("mode", ["room", "untiered", "system"])
async def test_the_verification_link_reactivates_a_deactivated_member_where_the_company_has_room_or_no_cap(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch, mode: str
):
    scenario = deletion_scenario
    identity_id = await _deactivated_unverified_target(scenario, monkeypatch, mode)

    await _click_verification_link(identity_id, monkeypatch)

    assert (await _fresh(scenario, "target")).is_active
    assert await _accounts(scenario) == ACCOUNTS


@pytest.mark.parametrize("mode", ["room", "untiered", "system"])
async def test_login_without_smtp_reactivates_a_deactivated_member_where_the_company_has_room_or_no_cap(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch, mode: str
):
    scenario = deletion_scenario
    identity_id = await _deactivated_unverified_target(scenario, monkeypatch, mode)

    await _log_in_without_smtp(identity_id, monkeypatch)

    assert (await _fresh(scenario, "target")).is_active
    assert await _accounts(scenario) == ACCOUNTS


async def _set_company_active(scenario: DeletionScenario, active: bool) -> None:
    _ = await tenant_lifecycle.set_tenant_active(await _company(scenario), is_active=active)


async def _active_names(scenario: DeletionScenario) -> set[str]:
    rows = await scenario.rows(
        "SELECT display_name FROM users WHERE tenant_id = %(id)s AND is_active AND role <> 'platform_admin'",
        {"id": scenario.tenant_id},
    )
    return {str(row["display_name"]) for row in rows}


async def test_re_enabling_a_capped_company_restores_organization_admins_first_up_to_its_free_slots(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)  # five accounts, no extras
    for who in ("owner", "other", "target"):
        await scenario.set_user(who, role="org_admin")  # six organization admins and one member
    await _set_company_active(scenario, False)
    assert await _accounts(scenario) == 0
    now = datetime.now(UTC)
    await scenario.set_user("genesis", updated_at=now - timedelta(days=2))
    for who in ("oa2", "oa3", "owner", "other", "target"):
        await scenario.set_user(who, updated_at=now - timedelta(days=1))

    await _set_company_active(scenario, True)

    assert await _active_names(scenario) == {"genesis", "oa2", "oa3", "owner", "other"}
    assert await _accounts(scenario) == 5


async def test_re_enabling_a_capped_company_restores_the_most_recently_updated_members_first(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)  # five accounts, no extras
    await _set_company_active(scenario, False)
    now = datetime.now(UTC)
    for who, hours in (("helper", 0), ("target", 1), ("owner", 24), ("other", 48)):
        await scenario.set_user(who, updated_at=now - timedelta(hours=hours))

    await _set_company_active(scenario, True)

    assert await _active_names(scenario) == {"genesis", "oa2", "oa3", "helper", "target"}
    assert await _accounts(scenario) == 5


@pytest.mark.parametrize("uncapped", ["untiered", "system"])
async def test_re_enabling_an_untiered_or_system_company_restores_every_member(
    deletion_scenario: DeletionScenario, uncapped: str
):
    scenario = deletion_scenario
    if uncapped == "system":
        _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    await _set_company_active(scenario, False)
    if uncapped == "system":
        await scenario.db.execute("UPDATE tenants SET is_system = true WHERE id = %(id)s", {"id": scenario.tenant_id})

    await _set_company_active(scenario, True)

    assert await _accounts(scenario) == ACCOUNTS  # Pro includes 5; no cap applies


async def test_repairing_the_genesis_admin_of_a_full_company_is_refused_until_it_has_a_free_account(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    grant_extras(monkeypatch, accounts=2)  # cap 7 == used
    await scenario.set_user("genesis", is_genesis=False)  # the company has no genesis admin any more
    repair = {"admin_email": "genesis2@example.test", "admin_password": "temp-password"}

    with pytest.raises(CompanyAccountLimitReachedError) as refused:
        _ = await tenant_provisioning.attach_genesis_org_admin(tenant_id=scenario.tenant_id, **repair)

    assert (refused.value.limit, refused.value.used) == (ACCOUNTS, ACCOUNTS)
    assert await identity_dao.get_by_email("genesis2@example.test") is None
    assert await _accounts(scenario) == ACCOUNTS

    grant_extras(monkeypatch, accounts=3)
    repaired = await tenant_provisioning.attach_genesis_org_admin(tenant_id=scenario.tenant_id, **repair)

    assert repaired.org_admin.is_genesis
    assert await _accounts(scenario) == ACCOUNTS + 1


@pytest.mark.parametrize("uncapped", ["untiered", "system"])
async def test_repairing_the_genesis_admin_of_an_untiered_or_system_company_takes_an_account_past_the_allocation(
    deletion_scenario: DeletionScenario, uncapped: str
):
    scenario = deletion_scenario
    if uncapped == "system":
        _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
        await scenario.db.execute("UPDATE tenants SET is_system = true WHERE id = %(id)s", {"id": scenario.tenant_id})
    await scenario.set_user("genesis", is_genesis=False)

    repaired = await tenant_provisioning.attach_genesis_org_admin(
        tenant_id=scenario.tenant_id, admin_email="genesis2@example.test", admin_password="temp-password"
    )

    assert repaired.org_admin.is_genesis
    assert await _accounts(scenario) == ACCOUNTS + 1  # Pro includes 5; no cap applies


# ── agents ──────────────────────────────────────────────────────────────


async def _create(scenario: DeletionScenario, who: str, name: str = "Fresh"):
    return await scenario.call(who, "POST", "/agents/", json={"name": name})


async def _seed(scenario: DeletionScenario, who: str, count: int) -> None:
    for index in range(count):
        _ = await scenario.agent(who, f"{who} {index}")


async def test_company_agent_cap_refuses_creation_while_the_per_account_cap_still_applies(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.MAX)  # 5 agents, 3 each
    _ = await scenario.agent("target", "Expired", is_expired=True)
    _ = await scenario.agent("target", "System", is_system=True)
    await _seed(scenario, "owner", 3)
    await _seed(scenario, "other", 2)
    assert await company_usage(scenario.db, scenario.tenant_id) == (ACCOUNTS, 5)

    company_full = await _create(scenario, "other")
    assert company_full.status_code == 403, company_full.text
    assert company_full.json()["detail"] == _agent_message(5)
    assert (await _create(scenario, "helper")).json()["detail"] == _agent_message(5)
    per_account = await _create(scenario, "owner")
    assert per_account.status_code == 403
    assert per_account.json()["detail"] == "Agent creation limit reached (3/3)."

    grant_extras(monkeypatch, agents=1)
    assert (await _create(scenario, "other")).status_code == 201
    again = await _create(scenario, "helper")
    assert (again.status_code, again.json()["detail"]) == (403, _agent_message(6))
    assert (await company_usage(scenario.db, scenario.tenant_id))[1] == 6


async def test_two_accounts_cannot_both_take_the_last_company_agent(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.MAX)
    await _seed(scenario, "owner", 2)
    await _seed(scenario, "other", 2)

    responses = await asyncio.gather(_create(scenario, "helper", "Racer A"), _create(scenario, "target", "Racer B"))

    assert sorted(response.status_code for response in responses) == [201, 403]
    assert (await company_usage(scenario.db, scenario.tenant_id))[1] == 5


async def test_renewing_an_expired_agent_at_the_company_cap_is_refused_until_a_slot_frees(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)  # 2 agents, 2 each
    first = await scenario.agent("owner", "First")
    _ = await scenario.agent("other", "Second")
    gone = await scenario.agent("target", "Gone", is_expired=True, status="stopped")
    renew: dict[str, object] = {"expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}

    refused = await scenario.call("oa2", "PATCH", f"/agents/{gone}", json=renew)
    assert (refused.status_code, refused.json()["detail"]) == (403, _agent_message(2))
    expired = await scenario.db.fetchval("SELECT is_expired FROM agents WHERE id = %(id)s", {"id": gone})
    assert expired is True

    await scenario.db.execute("DELETE FROM agents WHERE id = %(id)s", {"id": first})
    assert (await scenario.call("oa2", "PATCH", f"/agents/{gone}", json=renew)).status_code == 200


@pytest.mark.parametrize("uncapped", ["untiered", "system"])
async def test_untiered_and_system_organizations_have_no_company_agent_cap(
    deletion_scenario: DeletionScenario, uncapped: str
):
    scenario = deletion_scenario
    if uncapped == "system":
        _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
        await scenario.db.execute("UPDATE tenants SET is_system = true WHERE id = %(id)s", {"id": scenario.tenant_id})
    await _seed(scenario, "owner", 2)
    await _seed(scenario, "other", 2)
    await _seed(scenario, "target", 2)  # six agents, well past Pro's company cap of two

    assert (await _create(scenario, "helper")).status_code == 201
