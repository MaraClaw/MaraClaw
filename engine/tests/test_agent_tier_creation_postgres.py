"""Tier-governed ordinary-agent creation through the real ASGI stack and PostgreSQL."""

import asyncio
from uuid import UUID

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.records.organization_plan import OrganizationAgentLimit, OrganizationPlanId
from app.services.organization_plans import AmbiguousOrganizationTierError
from capacity_fixtures import grant_extras
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario"]

TIERS = [(OrganizationPlanId.PRO, 2), (OrganizationPlanId.MAX, 3), (OrganizationPlanId.ULTRA, 5)]


async def _create(scenario: DeletionScenario, who: str, name: str = "Fresh", tenant_id: UUID | None = None):
    body: dict[str, object] = {"name": name}
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return await scenario.call(who, "POST", "/agents/", json=body)


async def _count(scenario: DeletionScenario, who: str) -> int:
    value = await scenario.db.fetchval(
        "SELECT count(*) FROM agents WHERE creator_id = %(id)s", {"id": scenario[who].id}
    )
    assert isinstance(value, int)
    return value


@pytest.mark.parametrize(("tier", "cap"), TIERS)
async def test_account_gets_exactly_the_tier_cap_despite_a_stale_stored_quota(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch, tier: OrganizationPlanId, cap: int
):
    scenario = deletion_scenario
    grant_extras(monkeypatch, agents=20)  # isolate the per-account cap from the company-wide cap
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, tier)
    for index in range(cap - 1):
        _ = await scenario.agent("owner", f"Seed {index}")
    # The stored per-user allowance is 2 for every fixture user: Max and Ultra must not inherit it.
    last = await _create(scenario, "owner")
    assert last.status_code == 201, last.text
    over = await _create(scenario, "owner", "Over")
    assert over.status_code == 403
    assert over.json()["detail"] == f"Agent creation limit reached ({cap}/{cap})."
    assert await _count(scenario, "owner") == cap
    # Another account in the same organization is independent.
    assert (await _create(scenario, "other")).status_code == 201


async def test_stored_quota_neither_blocks_nor_raises_the_tier_cap(
    deletion_scenario: DeletionScenario, monkeypatch: pytest.MonkeyPatch
):
    scenario = deletion_scenario
    grant_extras(monkeypatch, agents=20)  # isolate the per-account cap from the company-wide cap
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    await scenario.set_user("owner", quota_max_agents=0)
    await scenario.set_user("other", quota_max_agents=99)
    assert [(await _create(scenario, "owner", f"A{n}")).status_code for n in range(3)] == [201, 201, 403]
    assert [(await _create(scenario, "other", f"B{n}")).status_code for n in range(3)] == [201, 201, 403]


@pytest.mark.parametrize("who", ["oa2", "helper", "genesis"])
async def test_administrators_are_capped_like_everyone_else(deletion_scenario: DeletionScenario, who: str):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    assert [(await _create(scenario, who, f"Admin{n}")).status_code for n in range(3)] == [201, 201, 403]


async def test_platform_admin_is_capped_only_by_the_explicit_tiered_destination(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    foreign = scenario.foreign_tenant_id
    await scenario.set_user("psa", tenant_id=foreign)
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    # Agents in the actor's own untiered organization do not count toward the destination.
    for index in range(3):
        _ = await scenario.agent("psa", f"Home {index}", tenant_id=foreign)
    codes = [(await _create(scenario, "psa", f"Away{n}", scenario.tenant_id)).status_code for n in range(3)]
    assert codes == [201, 201, 403]
    # Creating at home (untiered) keeps the administrator exemption.
    assert (await _create(scenario, "psa", "Home again", foreign)).status_code == 201


async def test_only_ordinary_nonexpired_agents_count_whatever_their_lifecycle_status(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.ULTRA)
    _ = await scenario.agent("owner", "System", is_system=True)
    _ = await scenario.agent("owner", "Expired", is_expired=True)
    _ = await scenario.agent("owner", "Foreign org", tenant_id=scenario.foreign_tenant_id)
    _ = await scenario.agent("owner", "Stopped", status="stopped")
    _ = await scenario.agent("owner", "Failed setup", status="error")
    pending = await scenario.agent("owner", "Pending delete")
    assert (await scenario.request("owner", pending)).status_code in {200, 201}
    # Three counted agents: two slots remain of five.
    assert [(await _create(scenario, "owner", f"New{n}")).status_code for n in range(3)] == [201, 201, 403]


async def test_unlinked_organizations_keep_the_stored_quota_and_system_agents_still_count(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await scenario.agent("owner", "System", is_system=True)
    assert (await _create(scenario, "owner", "One")).status_code == 201
    refused = await _create(scenario, "owner", "Two")
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Agent creation limit reached (2/2)."
    assert [(await _create(scenario, "oa2", f"Admin{n}")).status_code for n in range(4)] == [201] * 4


async def test_stored_snapshot_does_not_change_the_current_tier_cap(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    request_id = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.MAX)
    before = await scenario.rows(
        "SELECT plan FROM organization_admission_requests WHERE id = %(id)s", {"id": request_id}
    )
    for index in range(2):
        _ = await scenario.agent("owner", f"Seed {index}")
    assert [(await _create(scenario, "owner", f"New{n}")).status_code for n in range(2)] == [201, 403]
    after = await scenario.rows(
        "SELECT plan FROM organization_admission_requests WHERE id = %(id)s", {"id": request_id}
    )
    assert after == before


async def test_ambiguous_link_fails_closed_instead_of_falling_back_to_legacy_rules(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    for _ in range(2):
        _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.ULTRA)
    with pytest.raises(AmbiguousOrganizationTierError):
        _ = await _create(scenario, "owner")
    assert await _count(scenario, "owner") == 0


async def test_unreadable_tier_storage_fails_closed_without_a_partial_agent(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    await scenario.db.execute("DROP TABLE organization_admission_credential_emails")
    await scenario.db.execute("ALTER TABLE organization_admission_requests RENAME TO renamed_admission_requests")
    with pytest.raises(Exception, match="organization_admission_requests"):
        _ = await _create(scenario, "owner")
    assert await _count(scenario, "owner") == 0
    assert await scenario.rows("SELECT 1 FROM participants") == []


async def test_refused_creation_rolls_back_and_the_slot_returns_after_the_agent_is_removed(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    first = await scenario.agent("owner", "A")
    _ = await scenario.agent("owner", "B")
    assert (await _create(scenario, "owner")).status_code == 403
    assert await scenario.rows("SELECT 1 FROM agent_setups") == []
    await scenario.db.execute("DELETE FROM agents WHERE id = %(id)s", {"id": first})
    assert (await _create(scenario, "owner")).status_code == 201


async def test_concurrent_creations_cannot_both_take_the_last_tier_slot(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    cap = OrganizationAgentLimit.MAX
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.MAX)
    for index in range(cap - 1):
        _ = await scenario.agent("owner", f"Seed {index}")
    responses = await asyncio.gather(*(_create(scenario, "owner", f"Racer{n}") for n in range(4)))
    assert sorted(response.status_code for response in responses) == [201, 403, 403, 403]
    assert await _count(scenario, "owner") == cap


async def test_users_report_the_effective_cap_beside_the_unused_stored_quota(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    untiered = await scenario.call("genesis", "GET", f"/users/{scenario['owner'].id}")
    assert (untiered.json()["quota_max_agents"], untiered.json()["effective_max_agents"]) == (2, 2)
    admin = await scenario.call("genesis", "GET", f"/users/{scenario['oa2'].id}")
    assert admin.json()["effective_max_agents"] is None
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.ULTRA)
    edited = await scenario.call(
        "genesis", "PATCH", f"/users/{scenario['owner'].id}/quota", json={"quota_max_agents": 7}
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["quota_max_agents"], edited.json()["effective_max_agents"]) == (7, 5)
    admin = await scenario.call("genesis", "GET", f"/users/{scenario['oa2'].id}")
    assert admin.json()["effective_max_agents"] == 5
