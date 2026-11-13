"""Admin read and reset of a per-agent daily message count through the real ASGI stack and PostgreSQL."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from tests.daily_message_clock import TODAY, frozen_clock

from agent_deletion_fixtures import DeletionScenario, deletion_scenario
from app.dao.agent_daily_message_usage_dao import agent_daily_message_usage_dao
from app.dao.agent_dao import agent_dao
from app.dao.tenant_dao import tenant_dao
from app.records.organization_plan import OrganizationPlanId
from app.services.agent_message_quota import charge_agent_daily_message
from tier_limit_fixtures import approve_admission_for

__all__ = ["deletion_scenario", "frozen_clock"]

LIMIT = 100


def _reset_path(scenario: DeletionScenario, owner: str, agent_id: UUID) -> str:
    return f"/users/{scenario[owner].id}/agents/{agent_id}/daily-messages/reset"


async def _tiered_agent(scenario: DeletionScenario, used: int = 0, *, is_system: bool = False) -> UUID:
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    agent_id = await scenario.agent("owner", is_system=is_system)
    for _ in range(used):
        _ = await agent_daily_message_usage_dao.charge(agent_id, TODAY, LIMIT)
    return agent_id


async def _used(scenario: DeletionScenario, agent_id: UUID) -> int | None:
    value = await scenario.db.fetchval(
        "SELECT messages_used FROM agent_daily_message_usage WHERE agent_id = %(id)s", {"id": agent_id}
    )
    assert value is None or isinstance(value, int)
    return value


async def test_org_admin_resets_todays_count_and_the_action_is_audited(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await _tiered_agent(scenario, used=5)

    response = await scenario.call("oa2", "POST", _reset_path(scenario, "owner", agent_id))

    assert (response.status_code, response.json()) == (
        200,
        {"used": 0, "limit": LIMIT, "timezone": "UTC", "resets_at": "2026-11-14T00:00:00Z"},
    )
    assert await _used(scenario, agent_id) == 0
    audit = await scenario.rows(
        "SELECT actor_id, action, target_type, target_id, tenant_id, changes, details FROM admin_audit_logs"
    )
    assert audit == [
        {
            "actor_id": scenario["oa2"].id,
            "action": "agent_daily_messages_reset",
            "target_type": "agent",
            "target_id": agent_id,
            "tenant_id": scenario.tenant_id,
            "changes": {"messages_used": {"before": 5, "after": 0}},
            "details": {"user_id": str(scenario["owner"].id), "usage_date": "2026-11-13", "timezone": "UTC"},
        }
    ]
    assert await agent_daily_message_usage_dao.charge(agent_id, TODAY, LIMIT) == 1


async def test_platform_admin_resets_in_any_company_and_an_agent_without_usage_needs_no_row(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    agent_id = await _tiered_agent(scenario)

    response = await scenario.call("psa", "POST", _reset_path(scenario, "owner", agent_id))

    assert (response.status_code, response.json()["used"]) == (200, 0)
    assert await _used(scenario, agent_id) is None


async def test_an_earlier_days_row_is_left_alone_and_counts_as_zero(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await _tiered_agent(scenario)
    _ = await agent_daily_message_usage_dao.charge(agent_id, TODAY - timedelta(days=1), LIMIT)

    response = await scenario.call("oa2", "POST", _reset_path(scenario, "owner", agent_id))

    assert (response.status_code, response.json()["used"]) == (200, 0)
    assert await _used(scenario, agent_id) == 1
    audit = await scenario.rows("SELECT changes FROM admin_audit_logs")
    assert audit == [{"changes": {"messages_used": {"before": 0, "after": 0}}}]


@pytest.mark.parametrize("who", ["foreign_oa", "other", "helper", "owner"])
async def test_only_admins_of_the_same_company_may_reset(deletion_scenario: DeletionScenario, who: str):
    scenario = deletion_scenario
    agent_id = await _tiered_agent(scenario, used=3)

    response = await scenario.call(who, "POST", _reset_path(scenario, "owner", agent_id))

    assert response.status_code == 403
    assert await _used(scenario, agent_id) == 3
    assert await scenario.rows("SELECT 1 FROM admin_audit_logs") == []


async def test_an_untiered_organization_has_no_limit_to_reset(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await scenario.agent("owner")

    response = await scenario.call("oa2", "POST", _reset_path(scenario, "owner", agent_id))

    assert (response.status_code, response.json()["detail"]) == (409, "This agent has no daily message limit")
    assert await scenario.rows("SELECT 1 FROM admin_audit_logs") == []


async def test_a_system_agent_has_no_limit_to_reset(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await _tiered_agent(scenario, is_system=True)

    response = await scenario.call("oa2", "POST", _reset_path(scenario, "owner", agent_id))

    assert (response.status_code, response.json()["detail"]) == (409, "This agent has no daily message limit")


async def test_the_agent_must_be_one_the_user_created_in_the_users_company(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await approve_admission_for(scenario.db, scenario.tenant_id, OrganizationPlanId.PRO)
    others = await scenario.agent("other")
    abroad = await scenario.agent("owner", tenant_id=scenario.foreign_tenant_id)

    for agent_id in (others, abroad, uuid4()):
        response = await scenario.call("oa2", "POST", _reset_path(scenario, "owner", agent_id))
        assert (response.status_code, response.json()["detail"]) == (404, "Agent not found")
    unknown_user = await scenario.call("oa2", "POST", f"/users/{uuid4()}/agents/{others}/daily-messages/reset")
    assert (unknown_user.status_code, unknown_user.json()["detail"]) == (404, "User not found")


async def test_user_detail_reports_each_capped_agents_usage_in_the_genesis_admin_timezone(
    deletion_scenario: DeletionScenario,
):
    scenario = deletion_scenario
    await scenario.set_user("genesis", timezone="Asia/Ho_Chi_Minh")
    busy = await _tiered_agent(scenario)
    idle = await scenario.agent("owner", "Idle")
    system = await scenario.agent("owner", "System", is_system=True)
    stale = await scenario.agent("owner", "Stale")
    record = await agent_dao.get(busy)
    assert record is not None
    for _ in range(3):
        await charge_agent_daily_message(record)
    _ = await agent_daily_message_usage_dao.charge(stale, TODAY - timedelta(days=1), LIMIT)

    response = await scenario.call("oa2", "GET", f"/users/{scenario['owner'].id}")

    assert response.status_code == 200
    reported = {agent["id"]: agent["daily_messages"] for agent in response.json()["agents"]}
    window = {"limit": LIMIT, "timezone": "Asia/Ho_Chi_Minh", "resets_at": "2026-11-14T00:00:00+07:00"}
    assert reported == {
        str(busy): {"used": 3, **window},
        str(idle): {"used": 0, **window},
        str(stale): {"used": 0, **window},
        str(system): None,
    }


async def test_user_detail_has_no_daily_messages_in_an_untiered_organization(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    _ = await scenario.agent("owner")

    response = await scenario.call("oa2", "GET", f"/users/{scenario['owner'].id}")

    assert [agent["daily_messages"] for agent in response.json()["agents"]] == [None]


async def test_deleting_an_agent_or_its_company_removes_the_counter_rows(deletion_scenario: DeletionScenario):
    scenario = deletion_scenario
    agent_id = await _tiered_agent(scenario, used=2)
    abroad = await scenario.agent("owner", "Abroad", tenant_id=scenario.foreign_tenant_id)
    _ = await agent_daily_message_usage_dao.charge(abroad, TODAY, LIMIT)

    await agent_dao.delete_with_related(agent_id)
    await tenant_dao.delete_cascade(scenario.foreign_tenant_id)

    assert await scenario.rows("SELECT agent_id FROM agent_daily_message_usage") == []
    assert not await scenario.agent_exists(agent_id)
    assert not await scenario.agent_exists(abroad)
