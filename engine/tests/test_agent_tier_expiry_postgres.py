"""Renewing an expired agent is a net increase and honors the owner's tier cap (real ASGI stack and PostgreSQL)."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import Response

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.records.organization_plan import OrganizationPlanId
from capacity_fixtures import grant_extras
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario"]


def _expires_in(delta: timedelta) -> dict[str, object]:
    return {"expires_at": (datetime.now(UTC) + delta).isoformat()}


async def _renew(scenario: DeletionScenario, agent_id: UUID, who: str = "oa2", delta: timedelta = timedelta(days=1)):
    return await scenario.call(who, "PATCH", f"/agents/{agent_id}", json=_expires_in(delta))


async def _expired(scenario: DeletionScenario, owner: str, name: str = "Gone", *, is_system: bool = False) -> UUID:
    return await scenario.agent(owner, name, is_expired=True, status="stopped", is_system=is_system)


async def _state(scenario: DeletionScenario, agent_id: UUID) -> tuple[bool, str]:
    row = (await scenario.rows("SELECT is_expired, status FROM agents WHERE id = %(id)s", {"id": agent_id}))[0]
    return bool(row["is_expired"]), str(row["status"])


async def _counted(scenario: DeletionScenario, who: str) -> int:
    value = await scenario.db.fetchval(
        "SELECT count(*) FROM agents WHERE creator_id = %(id)s AND is_expired IS FALSE AND is_system IS FALSE",
        {"id": scenario[who].id},
    )
    assert isinstance(value, int)
    return value


def _detail(response: Response) -> object:
    return response.json()["detail"]


async def test_renewing_an_expired_agent_at_the_tier_cap_is_refused_and_leaves_it_expired(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("owner", "One")
    _ = await scenario.agent("owner", "Two")
    gone = await _expired(scenario, "owner")
    refused = await _renew(scenario, gone)
    assert refused.status_code == 403, refused.text
    assert _detail(refused) == "Agent creation limit reached (2/2)."
    assert await _state(scenario, gone) == (True, "stopped")
    assert await scenario.db.fetchval("SELECT expires_at FROM agents WHERE id = %(id)s", {"id": gone}) is None
    assert await _counted(scenario, "owner") == 2


async def test_renewing_with_room_succeeds_and_consumes_the_slot(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("owner", "One")
    gone = await _expired(scenario, "owner")
    renewed = await _renew(scenario, gone)
    assert renewed.status_code == 200, renewed.text
    assert await _state(scenario, gone) == (False, "idle")
    created = await scenario.call("owner", "POST", "/agents/", json={"name": "Over"})
    assert created.status_code == 403


async def test_the_owners_cap_governs_not_the_acting_administrators(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    grant_extras(monkeypatch, agents=20)  # isolate the per-account cap from the company-wide cap
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    for index in range(2):
        _ = await scenario.agent("owner", f"Full {index}")
    _ = await scenario.agent("oa2", "Admin one")
    owner_gone, spare_gone = await _expired(scenario, "owner"), await _expired(scenario, "other")
    assert (await _renew(scenario, owner_gone, "oa2")).status_code == 403
    assert (await _renew(scenario, owner_gone, "genesis")).status_code == 403
    # The administrator holds one agent and the other member none: room for the owner of the expired agent.
    assert (await _renew(scenario, spare_gone, "oa2")).status_code == 200


async def test_an_administrator_owner_is_capped_in_a_tiered_organization(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("oa3", "One")
    _ = await scenario.agent("oa3", "Two")
    gone = await _expired(scenario, "oa3")
    assert (await _renew(scenario, gone, "psa")).status_code == 403
    assert await _state(scenario, gone) == (True, "stopped")


async def test_concurrent_renewal_and_creation_cannot_both_take_the_last_slot(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("owner", "First")
    gone = await _expired(scenario, "owner")
    renewed, created = await asyncio.gather(
        _renew(scenario, gone), scenario.call("owner", "POST", "/agents/", json={"name": "Racer"})
    )
    assert sorted((renewed.status_code, created.status_code)) in ([200, 403], [201, 403])
    assert await _counted(scenario, "owner") == 2


async def test_concurrent_renewals_of_two_expired_agents_fill_one_slot_only(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("owner", "First")
    first, second = await _expired(scenario, "owner", "A"), await _expired(scenario, "owner", "B")
    results = await asyncio.gather(_renew(scenario, first), _renew(scenario, second, "genesis"))
    assert sorted(response.status_code for response in results) == [200, 403]
    assert await _counted(scenario, "owner") == 2


async def test_changes_without_a_net_increase_stay_allowed_at_the_cap(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    live = await scenario.agent("owner", "One")
    _ = await scenario.agent("owner", "Two")
    gone = await _expired(scenario, "owner")
    system_gone = await _expired(scenario, "owner", "System", is_system=True)
    assert (await _renew(scenario, live, delta=timedelta(days=30))).status_code == 200
    assert (await _renew(scenario, system_gone)).status_code == 200
    assert await _state(scenario, system_gone) == (False, "idle")
    past = await _renew(scenario, gone, delta=timedelta(days=-1))
    assert past.status_code == 200, past.text
    assert await _state(scenario, gone) == (True, "stopped")
    cleared = await scenario.call("oa2", "PATCH", f"/agents/{gone}", json={"expires_at": None})
    assert cleared.status_code == 403
    assert await _state(scenario, gone) == (True, "stopped")


async def test_an_account_over_its_cap_keeps_agents_but_cannot_renew_another(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    for index in range(3):
        _ = await scenario.agent("owner", f"Legacy overflow {index}")
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    gone = await _expired(scenario, "owner")
    assert (await _renew(scenario, gone)).status_code == 403
    assert await _counted(scenario, "owner") == 3


async def test_untiered_organizations_keep_their_legacy_renewal_behavior(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await scenario.agent("owner", "One")
    _ = await scenario.agent("owner", "Two")
    gone = await _expired(scenario, "owner")
    assert (await _renew(scenario, gone)).status_code == 200
    assert await _state(scenario, gone) == (False, "idle")
