import anyio
import pytest

from app.dao.tool_dao import agent_tool_dao
from app.records.tool import AgentToolRecord
from test_tool_config_operations_postgres import configured_tool
from tool_management_fixtures import ToolScenario, organization_scenario, tool_scenario

__all__ = ["organization_scenario", "tool_scenario"]


@pytest.mark.parametrize("admin_method", ["patch", "put"])
async def test_member_replacement_cannot_race_away_admin_network_policy(
    tool_scenario: ToolScenario, monkeypatch: pytest.MonkeyPatch, admin_method: str
) -> None:
    scenario = tool_scenario
    tool = await configured_tool("builtin", scenario)
    await agent_tool_dao.create(
        obj_in={"agent_id": scenario.agent.id, "tool_id": tool.id, "config": {"ordinary": "old"}}
    )
    await scenario.organization.db.execute(
        "UPDATE agents SET creator_id=%(user)s, access_mode='company' WHERE id=%(agent)s",
        {"user": scenario.organization.member.id, "agent": scenario.agent.id},
    )
    headers = scenario.organization.headers(scenario.organization.member)
    read, proceed = anyio.Event(), anyio.Event()
    original = agent_tool_dao.get_assignment
    intercepted = False

    async def pause_first(agent_id, tool_id) -> AgentToolRecord | None:
        nonlocal intercepted
        assignment = await original(agent_id, tool_id)
        if not intercepted:
            intercepted = True
            read.set()
            await proceed.wait()
        return assignment

    monkeypatch.setattr(agent_tool_dao, "get_assignment", pause_first)
    path = f"/api/tools/agents/{scenario.agent.id}/tool-config/{tool.id}"
    statuses: list[int] = []

    async def member_replacement() -> None:
        response = await scenario.client.put(path, headers=headers, json={"config": {"ordinary": "new"}})
        statuses.append(response.status_code)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(member_replacement)
        await read.wait()
        body = {"set": {"allow_network": True}} if admin_method == "patch" else {"config": {"allow_network": True}}
        response = await scenario.client.request(admin_method.upper(), path, headers=scenario.headers, json=body)
        assert response.status_code == 200
        proceed.set()
    assert statuses == [403]
    stored = await original(scenario.agent.id, tool.id)
    assert stored is not None
    assert stored.config["allow_network"] is True
