"""Approval-gated agent deletion through the real ASGI stack and PostgreSQL."""

import asyncio
from uuid import UUID

import pytest

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.api import background_tasks
from app.db.session import connection_ctx
from app.services import agent_deletion_approval

__all__ = ["deletion_scenario"]


async def _pending(scenario: DeletionScenario, who: str = "owner") -> tuple[UUID, str]:
    agent_id = await scenario.agent("owner")
    response = await scenario.request(who, agent_id)
    assert response.status_code == 201, response.text
    return agent_id, str(response.json()["id"])


async def _status(scenario: DeletionScenario, request_id: str) -> str:
    return str(
        await scenario.db.fetchval("SELECT status FROM agent_deletion_requests WHERE id = %(id)s", {"id": request_id})
    )


@pytest.mark.parametrize("actor", ["owner", "helper", "genesis"])
async def test_direct_delete_requires_a_request_even_for_the_creator(deletion_scenario: DeletionScenario, actor: str):
    scenario = deletion_scenario
    agent_id = await scenario.agent(actor)
    response = await scenario.call(actor, "DELETE", f"/agents/{agent_id}")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "agent_deletion_requires_request"
    await asyncio.sleep(0)
    assert await scenario.agent_exists(agent_id)
    assert scenario.runtime_calls == []


async def test_platform_admin_direct_delete_cleans_up_and_cancels_pending_request(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    response = await scenario.call("psa", "DELETE", f"/agents/{agent_id}")
    assert response.status_code == 204
    assert not await scenario.agent_exists(agent_id)
    row = await scenario.rows(
        "SELECT status, cancel_reason FROM agent_deletion_requests WHERE id = %(id)s", {"id": request_id}
    )
    assert row == [{"status": "cancelled", "cancel_reason": "agent_gone"}]


@pytest.mark.parametrize("reason", ["", "   \n\t ", "x" * 1001])
async def test_reason_must_be_nonblank_and_at_most_1000_characters(deletion_scenario: DeletionScenario, reason: str):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    assert (await scenario.request("owner", agent_id, reason)).status_code == 422
    assert await scenario.rows("SELECT id FROM agent_deletion_requests") == []


async def test_request_trims_reason_notifies_reviewers_without_the_private_reason(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner", "Secret Bot", mode="private")
    response = await scenario.request("owner", agent_id, "  contains confidential rationale  ")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reason"] == "contains confidential rationale"
    assert body["status"] == "pending"
    assert body["progress"] == {"counted_approvals": 0, "required_approvals": 2, "genesis_approved": False}
    assert body["requester"]["display_name"] == "owner"
    assert body["agent"]["is_private"] is True
    assert set(body["agent"]) == {
        "id",
        "name",
        "role_description",
        "access_mode",
        "status",
        "last_active_at",
        "creator",
        "is_private",
    }
    notes = await scenario.rows(
        "SELECT user_id, link, title, body, ref_id FROM notifications WHERE type = 'agent_deletion_requested'"
    )
    assert {note["user_id"] for note in notes} == {scenario["genesis"].id, scenario["oa2"].id, scenario["oa3"].id}
    assert {note["link"] for note in notes} == {f"/agent-deletion-requests/{body['id']}"}
    assert all("confidential" not in f"{note['title']} {note['body']}" for note in notes)


@pytest.mark.parametrize("actor", ["other", "psa", "foreign_oa"])
async def test_only_owner_or_company_admin_may_request(deletion_scenario: DeletionScenario, actor: str):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    assert (await scenario.request(actor, agent_id)).status_code == 403
    assert await scenario.rows("SELECT id FROM agent_deletion_requests") == []


async def test_company_admin_cannot_request_private_agent_or_system_agent(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    private_id = await scenario.agent("owner", mode="private")
    system_id = await scenario.agent("genesis", is_system=True)
    assert (await scenario.request("oa2", private_id)).status_code == 403
    assert (await scenario.request("genesis", system_id)).status_code == 403


async def test_company_admin_may_request_a_shared_agent_and_duplicate_request_conflicts(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    first = await scenario.request("oa2", agent_id)
    assert first.status_code == 201
    second = await scenario.request("owner", agent_id)
    assert second.status_code == 409
    assert second.json()["detail"]["request_id"] == first.json()["id"]


async def test_approval_requires_viewing_details_first(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    blocked = await scenario.approve("genesis", request_id, view=False)
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "detail_view_required"
    assert await scenario.agent_exists(agent_id)
    assert await scenario.rows("SELECT 1 FROM agent_deletion_request_approvals") == []


async def test_one_ordinary_admin_approval_keeps_the_request_pending(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    response = await scenario.approve("oa2", request_id)
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["progress"]["counted_approvals"], body["progress"]["genesis_approved"]) == (
        "pending",
        1,
        False,
    )
    assert body["viewer"] == {"can_approve": False, "has_viewed": True, "has_approved": True}
    assert await scenario.agent_exists(agent_id)
    assert scenario.runtime_calls == []


async def test_repeated_approval_is_idempotent_and_never_counts_twice(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _agent_id, request_id = await _pending(scenario)
    _ = await scenario.approve("oa2", request_id)
    again = await scenario.approve("oa2", request_id)
    assert again.status_code == 200
    assert again.json()["progress"]["counted_approvals"] == 1
    assert len(await scenario.rows("SELECT 1 FROM agent_deletion_request_approvals")) == 1


async def test_two_distinct_admins_execute_once_and_cleanup_runs_after_commit(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    _ = await scenario.approve("oa2", request_id)
    response = await scenario.approve("oa3", request_id)
    assert response.status_code == 200
    assert response.json()["status"] == "executed"
    repeat = await scenario.call("oa3", "POST", f"/agent-deletion-requests/{request_id}/approve")
    assert repeat.status_code == 200
    assert repeat.json()["status"] == "executed"
    await asyncio.sleep(0)
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert not await scenario.agent_exists(agent_id)
    assert scenario.runtime_calls == [("remove", agent_id), ("archive", agent_id)]
    stored = await scenario.rows("SELECT agent_name, status FROM agent_deletion_requests")
    assert stored == [{"agent_name": "Ops Bot", "status": "executed"}]
    notes = await scenario.rows("SELECT user_id, link FROM notifications WHERE type = 'agent_deletion_resolved'")
    assert notes == [{"user_id": scenario["owner"].id, "link": "/app/agents"}]


async def test_active_genesis_admin_alone_executes(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    response = await scenario.approve("genesis", request_id)
    assert response.json()["status"] == "executed"
    assert response.json()["progress"]["genesis_approved"] is True
    assert not await scenario.agent_exists(agent_id)


async def test_requesting_admin_counts_toward_the_threshold(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")
    created = await scenario.request("oa2", agent_id)
    request_id = created.json()["id"]
    _ = await scenario.approve("oa2", request_id)
    response = await scenario.approve("oa3", request_id)
    assert response.json()["status"] == "executed"
    assert not await scenario.agent_exists(agent_id)


async def test_retention_conflict_at_execution_persists_no_approval_and_keeps_the_agent(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    owner = scenario["owner"]
    transfer_id = await scenario.db.fetchval(
        "INSERT INTO company_transfer_requests (id, applicant_user_id, source_tenant_id, target_tenant_id, choice,"
        " method, fee_amount_minor, currency, instructions, free_consent_at)"
        " VALUES (gen_random_uuid(), %(user)s, %(source)s, %(target)s, 'free', 'invitation', 0, 'USD', 'x', now())"
        " RETURNING id",
        {"user": owner.id, "source": owner.tenant_id, "target": scenario.foreign_tenant_id},
    )
    await scenario.db.execute(
        "INSERT INTO company_transfer_retained_agents (agent_id, transfer_request_id, original_user_id,"
        " source_tenant_id, holder_user_id) VALUES (%(agent)s, %(transfer)s, %(user)s, %(source)s, %(user)s)",
        {"agent": agent_id, "transfer": transfer_id, "user": owner.id, "source": owner.tenant_id},
    )
    response = await scenario.approve("genesis", request_id)
    assert (response.status_code, response.json()["detail"]["code"]) == (409, "transfer_retention_conflict")
    assert await scenario.rows("SELECT 1 FROM agent_deletion_request_approvals") == []
    assert await _status(scenario, request_id) == "pending"
    assert await scenario.agent_exists(agent_id)


@pytest.mark.parametrize(
    ("voter", "viewed_status", "approved_status"), [("foreign_oa", 404, 404), ("owner", 200, 403), ("psa", 404, 403)]
)
async def test_non_company_admins_cannot_approve(
    deletion_scenario: DeletionScenario, voter: str, viewed_status: int, approved_status: int
):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    viewed = await scenario.call(voter, "GET", f"/agent-deletion-requests/{request_id}")
    approved = await scenario.call(voter, "POST", f"/agent-deletion-requests/{request_id}/approve")
    assert (viewed.status_code, approved.status_code) == (viewed_status, approved_status)
    assert await scenario.agent_exists(agent_id)
    assert await scenario.rows("SELECT 1 FROM agent_deletion_request_approvals") == []


async def test_inactive_demoted_or_password_gated_approvers_do_not_count(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    _ = await scenario.approve("oa2", request_id)
    await scenario.set_user("oa2", role="member")
    viewed = await scenario.call("oa3", "GET", f"/agent-deletion-requests/{request_id}")
    assert viewed.status_code == 200
    assert viewed.json()["approvals"][0]["counted"] is False
    approved = await scenario.call("oa3", "POST", f"/agent-deletion-requests/{request_id}/approve")
    assert approved.json()["status"] == "pending"
    assert approved.json()["progress"]["counted_approvals"] == 1
    assert await scenario.agent_exists(agent_id)
    await scenario.db.execute(
        "UPDATE identities SET must_change_password = true WHERE id = %(id)s", {"id": scenario["oa3"].identity_id}
    )
    blocked = await scenario.call("oa3", "POST", f"/agent-deletion-requests/{request_id}/approve")
    assert blocked.status_code == 403
    assert await scenario.agent_exists(agent_id)


async def test_deactivated_previous_approver_is_not_counted_at_execution(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    _ = await scenario.approve("oa2", request_id)
    await scenario.set_user("oa2", is_active=False)
    response = await scenario.approve("oa3", request_id)
    assert response.json()["status"] == "pending"
    assert await scenario.agent_exists(agent_id)
    assert scenario.runtime_calls == []


async def test_concurrent_final_approvals_execute_exactly_once(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    for voter in ("oa2", "oa3"):
        assert (await scenario.call(voter, "GET", f"/agent-deletion-requests/{request_id}")).status_code == 200
    first, second = await asyncio.gather(
        scenario.call("oa2", "POST", f"/agent-deletion-requests/{request_id}/approve"),
        scenario.call("oa3", "POST", f"/agent-deletion-requests/{request_id}/approve"),
    )
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert (first.status_code, second.status_code) == (200, 200)
    assert sorted([first.json()["status"], second.json()["status"]]) == ["executed", "pending"]
    assert not await scenario.agent_exists(agent_id)
    assert scenario.runtime_calls == [("remove", agent_id), ("archive", agent_id)]
    assert len(await scenario.rows("SELECT 1 FROM agent_deletion_request_approvals")) == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("UPDATE agents SET creator_id = %(other)s WHERE id = %(agent)s", "owner_changed"),
        ("UPDATE agents SET tenant_id = %(foreign)s WHERE id = %(agent)s", "tenant_changed"),
        ("DELETE FROM agents WHERE id = %(agent)s", "agent_gone"),
    ],
)
async def test_stale_request_is_cancelled_instead_of_deleting(
    deletion_scenario: DeletionScenario, change: str, reason: str
):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    await scenario.db.execute(
        change, {"other": scenario["other"].id, "foreign": scenario.foreign_tenant_id, "agent": agent_id}
    )
    response = await scenario.approve("genesis", request_id)
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["cancel_reason"]) == ("cancelled", reason)
    assert scenario.runtime_calls == []


async def test_requester_can_withdraw_and_others_cannot(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    assert (await scenario.call("oa2", "POST", f"/agent-deletion-requests/{request_id}/cancel")).status_code == 404
    response = await scenario.call("owner", "POST", f"/agent-deletion-requests/{request_id}/cancel")
    assert (response.status_code, response.json()["cancel_reason"]) == (200, "withdrawn")
    assert (await scenario.call("owner", "POST", f"/agent-deletion-requests/{request_id}/cancel")).status_code == 200
    assert await scenario.agent_exists(agent_id)


async def test_list_scopes_to_requester_or_company_admin_with_filters_and_cursor(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    ids = []
    for index in range(3):
        agent_id = await scenario.agent("owner", f"Bot {index}")
        ids.append((agent_id, (await scenario.request("owner", agent_id)).json()["id"]))
    foreign_agent = await scenario.agent("other", "Theirs")
    _ = await scenario.request("other", foreign_agent)
    mine = (await scenario.call("owner", "GET", "/agent-deletion-requests")).json()
    assert len(mine["items"]) == 3
    assert mine["next_cursor"] is None
    everything = (await scenario.call("genesis", "GET", "/agent-deletion-requests?limit=2")).json()
    assert len(everything["items"]) == 2
    assert everything["next_cursor"]
    rest = (
        await scenario.call("genesis", "GET", f"/agent-deletion-requests?limit=2&cursor={everything['next_cursor']}")
    ).json()
    assert len(rest["items"]) == 2
    assert rest["next_cursor"] is None
    filtered = (
        await scenario.call("genesis", "GET", f"/agent-deletion-requests?agent_id={ids[0][0]}&status=pending")
    ).json()
    assert [item["id"] for item in filtered["items"]] == [ids[0][1]]
    mismatch = await scenario.call(
        "genesis", "GET", f"/agent-deletion-requests?cursor={everything['next_cursor']}&status=pending"
    )
    assert mismatch.status_code == 400
    outsider = (await scenario.call("foreign_oa", "GET", "/agent-deletion-requests")).json()
    assert outsider["items"] == []


async def test_detail_records_company_admin_views_only(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _agent_id, request_id = await _pending(scenario)
    own = await scenario.call("owner", "GET", f"/agent-deletion-requests/{request_id}")
    assert own.json()["viewer"]["has_viewed"] is False
    assert await scenario.rows("SELECT 1 FROM agent_deletion_request_views") == []
    reviewer = await scenario.call("oa2", "GET", f"/agent-deletion-requests/{request_id}")
    assert reviewer.json()["viewer"] == {"can_approve": True, "has_viewed": True, "has_approved": False}
    assert (await scenario.call("other", "GET", f"/agent-deletion-requests/{request_id}")).status_code == 404


async def test_rolled_back_execution_keeps_agent_and_defers_cleanup(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id, request_id = await _pending(scenario)
    assert (await scenario.call("genesis", "GET", f"/agent-deletion-requests/{request_id}")).status_code == 200

    async def failing_request() -> None:
        async with connection_ctx():
            detail = await agent_deletion_approval.approve(UUID(request_id), scenario["genesis"])
            assert detail.status == "executed"
            raise RuntimeError("later failure")

    with pytest.raises(RuntimeError, match="later failure"):
        await failing_request()
    await asyncio.gather(*tuple(background_tasks._tasks))
    assert await scenario.agent_exists(agent_id)
    assert await _status(scenario, request_id) == "pending"
    assert scenario.runtime_calls == []
