from unittest.mock import AsyncMock

import anyio
import pytest
from httpx import Response

from app.core.json_types import JsonObject, json_object_from
from app.dao.tenant_dao import tenant_dao
from app.dao.tool_dao import tool_dao
from app.db.session import get_connection
from app.services.mcp_client import MCPClient
from app.services.tool_config import decrypt_sensitive_fields
from tool_management_fixtures import ToolScenario, organization_scenario, tool_scenario

__all__ = ["organization_scenario", "tool_scenario"]

INVENTORY: list[JsonObject] = [
    {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
    {"name": "fetch", "description": "Fetch", "inputSchema": {}},
]


@pytest.fixture
def discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    async def remote(_self: MCPClient) -> list[JsonObject]:
        assert get_connection() is None
        return INVENTORY

    monkeypatch.setattr(MCPClient, "list_tools", remote)
    monkeypatch.setattr("app.api.tool_catalog_import.validate_public_url", AsyncMock(side_effect=lambda url: url))


def payload(prefix: str = "qa") -> JsonObject:
    return {
        "server_url": "https://mcp.example.com/mcp",
        "api_key": "catalog-secret-1234",
        "server_name": "QA",
        "name_prefix": prefix,
        "tools": [dict(tool) for tool in INVENTORY],
    }


async def test_discover_and_import_company_catalog_without_assignments(
    tool_scenario: ToolScenario, discovery: None
) -> None:
    scenario = tool_scenario
    response = await scenario.client.post(
        "/api/tools/mcp/discover", headers=scenario.headers, json={"server_url": "https://mcp.example.com/mcp"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "tools": INVENTORY}
    response = await scenario.client.post("/api/tools/mcp/import", headers=scenario.headers, json=payload())
    assert response.status_code == 200
    rows = await scenario.organization.db.fetchall("SELECT * FROM tools ORDER BY name")
    assert [row["name"] for row in rows] == ["qa_fetch", "qa_search"]
    assert all(row["source"] == "admin" and row["tenant_id"] == scenario.agent.tenant_id for row in rows)
    for row in rows:
        config = json_object_from(row["config"])
        assert config["api_key"] != "catalog-secret-1234"
        assert decrypt_sensitive_fields(config)["api_key"] == "catalog-secret-1234"
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM agent_tools") == 0
    assert "catalog-secret" not in response.text


@pytest.mark.parametrize("source", ["builtin", "admin", "agent"])
async def test_changed_selection_collision_and_retry_are_atomic(
    tool_scenario: ToolScenario, discovery: None, source: str
) -> None:
    scenario = tool_scenario
    changed = payload()
    changed["tools"] = [{"name": "search", "description": "Changed", "inputSchema": {}}]
    response = await scenario.client.post("/api/tools/mcp/import", headers=scenario.headers, json=changed)
    assert response.status_code == 409
    existing = await tool_dao.create(obj_in={"name": "qa_fetch", "source": source, "config": {"retained": True}})
    if source == "agent":
        from app.dao.tool_dao import agent_tool_dao

        await agent_tool_dao.create(obj_in={"agent_id": scenario.agent.id, "tool_id": existing.id, "source": "agent"})
    response = await scenario.client.post("/api/tools/mcp/import", headers=scenario.headers, json=payload())
    assert response.status_code == 409
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM tools") == 1
    stored = await tool_dao.get(existing.id)
    assert stored is not None
    assert stored.config == {"retained": True}
    assert stored.source == source
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM agent_tools") == (
        1 if source == "agent" else 0
    )


async def test_concurrent_imports_publish_exactly_one_complete_batch(
    tool_scenario: ToolScenario, discovery: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tool_scenario
    responses: list[Response] = []
    other = await tenant_dao.create(obj_in={"name": "Other", "slug": "other"})
    ready = anyio.Event()
    reached = 0
    original = tool_dao.list_by_names

    async def synchronized_preflight(names):
        nonlocal reached
        rows = await original(names)
        reached += 1
        if reached == 2:
            ready.set()
        await ready.wait()
        return rows

    monkeypatch.setattr(tool_dao, "list_by_names", synchronized_preflight)

    async def submit(headers: dict[str, str], data: JsonObject) -> None:
        responses.append(await scenario.client.post("/api/tools/mcp/import", headers=headers, json=data))

    foreign = payload()
    foreign["tenant_id"] = str(other.id)
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(submit, scenario.headers, payload())
        tasks.start_soon(submit, scenario.organization.headers(scenario.organization.additional), foreign)
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM tools") == 2
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM agent_tools") == 0


@pytest.mark.parametrize(
    ("statement", "value", "status"),
    [
        ("UPDATE users SET role=%(value)s WHERE id=%(id)s", "member", 403),
        ("UPDATE users SET is_active=%(value)s WHERE id=%(id)s", False, 401),
        (
            "UPDATE identities SET must_change_password=%(value)s WHERE id=(SELECT identity_id FROM users WHERE id=%(id)s)",
            True,
            403,
        ),
        (
            "UPDATE identities SET auth_generation=auth_generation+1 WHERE id=(SELECT identity_id FROM users WHERE id=%(id)s) AND %(value)s",
            True,
            401,
        ),
        ("UPDATE tenants SET is_active=%(value)s WHERE id=(SELECT tenant_id FROM users WHERE id=%(id)s)", False, 403),
    ],
)
async def test_actor_revocation_during_remote_work_prevents_import(
    tool_scenario: ToolScenario,
    discovery: None,
    monkeypatch: pytest.MonkeyPatch,
    statement: str,
    value: object,
    status: int,
) -> None:
    scenario = tool_scenario

    async def revoked(_self: MCPClient) -> list[JsonObject]:
        assert get_connection() is None
        await scenario.organization.db.execute(statement, {"value": value, "id": scenario.organization.org_genesis.id})
        return INVENTORY

    monkeypatch.setattr(MCPClient, "list_tools", revoked)
    response = await scenario.client.post("/api/tools/mcp/import", headers=scenario.headers, json=payload())
    assert response.status_code == status
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM tools") == 0


async def test_insert_failure_rolls_back_previous_rows(
    tool_scenario: ToolScenario, discovery: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tool_scenario
    create = tool_dao.create

    async def fail_second(*, obj_in):
        if obj_in["name"] == "qa_fetch":
            raise RuntimeError("injected persistence failure")
        return await create(obj_in=obj_in)

    monkeypatch.setattr(tool_dao, "create", fail_second)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await scenario.client.post("/api/tools/mcp/import", headers=scenario.headers, json=payload())
    assert await scenario.organization.db.fetchval("SELECT count(*) FROM tools") == 0


async def test_members_and_foreign_targets_are_denied(tool_scenario: ToolScenario, discovery: None) -> None:
    scenario = tool_scenario
    response = await scenario.client.post(
        "/api/tools/mcp/import", headers=scenario.organization.headers(scenario.organization.member), json=payload()
    )
    assert response.status_code == 403
    foreign = payload()
    foreign["tenant_id"] = str(scenario.organization.genesis.id)
    response = await scenario.client.post("/api/tools/mcp/import", headers=scenario.headers, json=foreign)
    assert response.status_code == 403
