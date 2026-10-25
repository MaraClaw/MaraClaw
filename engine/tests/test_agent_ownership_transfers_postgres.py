"""Same-company agent ownership changes through the real ASGI stack and PostgreSQL."""

import asyncio
from uuid import UUID, uuid4

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario

__all__ = ["deletion_scenario"]


async def _transfer(
    scenario: DeletionScenario, agent_ids: list[UUID], source: str = "owner", target: str = "target", actor: str = "oa2"
):
    return await scenario.call(
        actor,
        "POST",
        "/agent-ownership-transfers",
        json={
            "from_user_id": str(scenario[source].id),
            "to_user_id": str(scenario[target].id),
            "agent_ids": [str(agent_id) for agent_id in agent_ids],
        },
    )


async def _creator(scenario: DeletionScenario, agent_id: UUID) -> UUID:
    row = await scenario.rows("SELECT creator_id FROM agents WHERE id = %(id)s", {"id": agent_id})
    return UUID(str(row[0]["creator_id"]))


async def test_org_admin_moves_company_and_private_agents_atomically(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    shared = await scenario.agent("owner", "Shared")
    private = await scenario.agent("owner", "Private", mode="private")
    response = await _transfer(scenario, [shared, private])
    assert response.status_code == 200, response.text
    assert response.json() == {"transferred": [str(shared), str(private)], "cancelled_deletion_request_ids": []}
    assert {await _creator(scenario, shared), await _creator(scenario, private)} == {scenario["target"].id}
    grants = await scenario.rows(
        "SELECT scope_id, access_level FROM agent_permissions WHERE agent_id = %(id)s", {"id": private}
    )
    assert grants == [{"scope_id": scenario["target"].id, "access_level": "manage"}]
    assert (await scenario.call("owner", "GET", f"/agents/{private}")).status_code == 403
    assert (await scenario.call("target", "GET", f"/agents/{private}")).status_code == 200
    assert (await scenario.call("genesis", "GET", f"/agents/{private}")).status_code == 403


async def test_deactivated_source_can_be_offboarded_without_starting_runtime(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner", status="stopped")
    await scenario.set_user("owner", is_active=False)
    response = await _transfer(scenario, [agent_id])
    assert response.status_code == 200, response.text
    assert await _creator(scenario, agent_id) == scenario["target"].id
    assert await scenario.rows("SELECT status FROM agents WHERE id = %(id)s", {"id": agent_id}) == [
        {"status": "stopped"}
    ]
    assert scenario.runtime_calls == []


@pytest.mark.parametrize(
    ("source", "target", "actor", "expected"),
    [
        ("owner", "foreign_oa", "oa2", 404),
        ("foreign_oa", "target", "oa2", 404),
        ("owner", "oa3", "oa2", 422),
        ("owner", "owner", "oa2", 422),
        ("owner", "target", "other", 403),
        ("owner", "target", "foreign_oa", 404),
    ],
)
async def test_invalid_parties_are_rejected_without_writes(
    deletion_scenario: DeletionScenario, source: str, target: str, actor: str, expected: int
):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    response = await _transfer(scenario, [agent_id], source, target, actor)
    assert response.status_code == expected, response.text
    assert await _creator(scenario, agent_id) == scenario["owner"].id


async def test_inactive_target_and_duplicate_agents_are_rejected(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    await scenario.set_user("target", is_active=False)
    assert (await _transfer(scenario, [agent_id])).status_code == 409
    await scenario.set_user("target", is_active=True)
    assert (await _transfer(scenario, [agent_id, agent_id])).status_code == 422
    assert await _creator(scenario, agent_id) == scenario["owner"].id


@pytest.mark.parametrize(
    ("bad", "expected"), [("system", 403), ("foreign_tenant", 409), ("other_owner", 409), ("missing", 404)]
)
async def test_one_invalid_agent_prevents_every_transfer(deletion_scenario: DeletionScenario, bad: str, expected: int):
    scenario = deletion_scenario
    good = await scenario.agent("owner", "Good")
    bad_id = {
        "system": await scenario.agent("owner", "System", is_system=True),
        "foreign_tenant": await scenario.agent("owner", "Foreign", tenant_id=scenario.foreign_tenant_id),
        "other_owner": await scenario.agent("other", "Theirs"),
        "missing": uuid4(),
    }[bad]
    response = await _transfer(scenario, [good, bad_id])
    assert response.status_code == expected, response.text
    assert await _creator(scenario, good) == scenario["owner"].id


async def test_transfer_cannot_exceed_the_new_owners_agent_quota(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await scenario.agent("target", "Already mine")
    first, second = await scenario.agent("owner", "A"), await scenario.agent("owner", "B")
    expired = await scenario.agent("owner", "Expired", is_expired=True)
    over = await _transfer(scenario, [first, second])
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "target_quota_exceeded"
    assert await _creator(scenario, first) == scenario["owner"].id
    within = await _transfer(scenario, [first, expired])
    assert within.status_code == 200, within.text
    assert await _creator(scenario, expired) == scenario["target"].id


async def test_ownership_change_cancels_pending_deletion_request(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    request_id = (await scenario.request("owner", agent_id)).json()["id"]
    response = await _transfer(scenario, [agent_id])
    assert response.json()["cancelled_deletion_request_ids"] == [request_id]
    stored = await scenario.rows("SELECT status, cancel_reason FROM agent_deletion_requests")
    assert stored == [{"status": "cancelled", "cancel_reason": "owner_changed"}]
    approval = await scenario.approve("genesis", request_id)
    assert (approval.status_code, approval.json()["detail"]["code"]) == (409, "deletion_request_stale")
    assert await scenario.agent_exists(agent_id)


async def test_concurrent_transfer_and_final_approval_leave_one_consistent_outcome(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    request_id = (await scenario.request("owner", agent_id)).json()["id"]
    assert (await scenario.call("genesis", "GET", f"/agent-deletion-requests/{request_id}")).status_code == 200
    approval, transfer = await asyncio.gather(
        scenario.call("genesis", "POST", f"/agent-deletion-requests/{request_id}/approve"),
        _transfer(scenario, [agent_id]),
    )
    status = (await scenario.rows("SELECT status FROM agent_deletion_requests"))[0]["status"]
    if status == "executed":
        assert not await scenario.agent_exists(agent_id)
        assert transfer.status_code in {404, 409}
    else:
        assert status == "cancelled"
        assert approval.json()["status"] == "cancelled"
        assert await _creator(scenario, agent_id) == scenario["target"].id


async def test_creator_handover_requires_active_same_company_target(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    request_id = (await scenario.request("owner", agent_id)).json()["id"]
    path = f"/agents/{agent_id}/handover"
    foreign = await scenario.call("owner", "POST", path, json={"new_creator_id": str(scenario["foreign_oa"].id)})
    assert foreign.status_code == 404
    await scenario.set_user("target", is_active=False)
    inactive = await scenario.call("owner", "POST", path, json={"new_creator_id": str(scenario["target"].id)})
    assert inactive.status_code == 409
    assert await _creator(scenario, agent_id) == scenario["owner"].id
    await scenario.set_user("target", is_active=True)
    done = await scenario.call("owner", "POST", path, json={"new_creator_id": str(scenario["target"].id)})
    assert done.status_code == 200, done.text
    assert done.json()["new_creator"] == "target"
    assert await _creator(scenario, agent_id) == scenario["target"].id
    assert await scenario.rows(
        "SELECT cancel_reason FROM agent_deletion_requests WHERE id = %(id)s", {"id": request_id}
    ) == [{"cancel_reason": "owner_changed"}]


async def test_creator_handover_still_creator_only_and_respects_quota(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    _ = await scenario.agent("target", "One")
    _ = await scenario.agent("target", "Two")
    path = f"/agents/{agent_id}/handover"
    assert (
        await scenario.call("other", "POST", path, json={"new_creator_id": str(scenario["target"].id)})
    ).status_code == 403
    full = await scenario.call("owner", "POST", path, json={"new_creator_id": str(scenario["target"].id)})
    assert full.status_code == 409
    assert await _creator(scenario, agent_id) == scenario["owner"].id


async def test_user_detail_reports_whether_each_agent_is_a_system_agent(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    normal = await scenario.agent("owner", "Normal")
    system = await scenario.agent("owner", "System", is_system=True)
    response = await scenario.call("genesis", "GET", f"/users/{scenario['owner'].id}")
    assert response.status_code == 200, response.text
    flags = {agent["id"]: agent["is_system"] for agent in response.json()["agents"]}
    assert flags == {str(normal): False, str(system): True}
