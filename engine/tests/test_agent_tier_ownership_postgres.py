"""Tier-governed incoming agent ownership through the real ASGI stack and PostgreSQL."""

import asyncio
from uuid import UUID

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.records.organization_plan import OrganizationPlanId
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario"]


async def _transfer(scenario: DeletionScenario, agent_ids: list[UUID], target: str = "target"):
    return await scenario.call(
        "oa2",
        "POST",
        "/agent-ownership-transfers",
        json={
            "from_user_id": str(scenario["owner"].id),
            "to_user_id": str(scenario[target].id),
            "agent_ids": [str(agent_id) for agent_id in agent_ids],
        },
    )


async def _handover(scenario: DeletionScenario, agent_id: UUID, target: str):
    return await scenario.call(
        "owner", "POST", f"/agents/{agent_id}/handover", json={"new_creator_id": str(scenario[target].id)}
    )


async def _owner_of(scenario: DeletionScenario, agent_id: UUID) -> UUID:
    owner = await scenario.db.fetchval("SELECT creator_id FROM agents WHERE id = %(id)s", {"id": agent_id})
    assert isinstance(owner, UUID)
    return owner


async def test_transfer_fills_exactly_to_the_tier_cap_and_refuses_the_overflow_atomically(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.ULTRA)
    # The stored allowance (2) is stale: the recipient holds 3 and may take two more, not zero.
    for index in range(3):
        _ = await scenario.agent("target", f"Mine {index}")
    batch = [await scenario.agent("owner", f"Move {index}") for index in range(3)]
    over = await _transfer(scenario, batch)
    assert over.status_code == 409
    assert over.json()["detail"] == {"code": "target_quota_exceeded", "message": "Agent limit reached (3+3/5)."}
    assert {await _owner_of(scenario, agent_id) for agent_id in batch} == {scenario["owner"].id}
    fit = await _transfer(scenario, batch[:2])
    assert fit.status_code == 200, fit.text
    assert {await _owner_of(scenario, agent_id) for agent_id in batch[:2]} == {scenario["target"].id}


async def test_only_ordinary_nonexpired_agents_count_and_expired_incoming_agents_are_free(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("target", "System", is_system=True)
    _ = await scenario.agent("target", "Expired", is_expired=True)
    _ = await scenario.agent("target", "Foreign", tenant_id=scenario.foreign_tenant_id)
    _ = await scenario.agent("target", "Stopped", status="stopped")
    live, expired = await scenario.agent("owner", "Live"), await scenario.agent("owner", "Gone", is_expired=True)
    assert (await _transfer(scenario, [live, expired])).status_code == 200


async def test_an_account_already_over_its_cap_keeps_agents_but_takes_only_zero_increase_transfers(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    for index in range(3):
        _ = await scenario.agent("target", f"Legacy overflow {index}")
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    ordinary, expired = await scenario.agent("owner", "Live"), await scenario.agent("owner", "Gone", is_expired=True)
    assert (await _transfer(scenario, [ordinary])).json()["detail"]["code"] == "target_quota_exceeded"
    assert (await _transfer(scenario, [expired])).status_code == 200
    kept = await scenario.rows(
        "SELECT count(*) AS n FROM agents WHERE creator_id = %(id)s", {"id": scenario["target"].id}
    )
    assert kept[0]["n"] == 4


@pytest.mark.parametrize("target", ["oa3", "genesis", "helper"])
async def test_handover_to_an_administrator_is_capped_in_a_tiered_organization(
    deletion_scenario: DeletionScenario, target: str
):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent(target, "One")
    _ = await scenario.agent(target, "Two")
    agent_id = await scenario.agent("owner", "Handed")
    refused = await _handover(scenario, agent_id, target)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "target_quota_exceeded"
    assert await _owner_of(scenario, agent_id) == scenario["owner"].id


async def test_handover_to_an_administrator_stays_exempt_without_a_tier_link(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    for index in range(3):
        _ = await scenario.agent("oa3", f"Admin {index}")
    agent_id = await scenario.agent("owner", "Handed")
    assert (await _handover(scenario, agent_id, "oa3")).status_code == 200


async def test_concurrent_creation_and_transfer_cannot_both_take_the_last_slot(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    _ = await scenario.agent("target", "First")
    incoming = await scenario.agent("owner", "Incoming")
    created, moved = await asyncio.gather(
        scenario.call("target", "POST", "/agents/", json={"name": "Racer"}), _transfer(scenario, [incoming])
    )
    assert sorted((created.status_code, moved.status_code)) in ([200, 403], [201, 409])
    held = await scenario.rows(
        "SELECT count(*) AS n FROM agents WHERE creator_id = %(id)s", {"id": scenario["target"].id}
    )
    assert held[0]["n"] == 2
