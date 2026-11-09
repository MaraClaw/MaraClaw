from unittest.mock import AsyncMock

import anyio
import pytest

from app.core.json_types import JsonObject, json_object_from
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.db.session import get_connection
from app.records.tool import ToolRecord
from app.services.mcp_client import MCPClient
from app.services.tool_config import decrypt_sensitive_fields, encrypt_sensitive_fields
from tool_management_fixtures import ToolScenario, organization_scenario, tool_scenario

__all__ = ["organization_scenario", "tool_scenario"]


async def configured_tool(source: str, scenario: ToolScenario) -> ToolRecord:
    return await tool_dao.create(
        obj_in={
            "name": "configured",
            "type": "mcp",
            "source": source,
            "tenant_id": scenario.agent.tenant_id if source == "admin" else None,
            "config": encrypt_sensitive_fields({"api_key": "original-secret", "other": "keep", "removable": "remove"}),
            "mcp_server_url": "https://mcp.example.com/mcp",
        }
    )


@pytest.mark.parametrize("source", ["builtin", "admin"])
async def test_company_operations_preserve_secret_and_remove_only_selected_fields(
    tool_scenario: ToolScenario, source: str
) -> None:
    scenario = tool_scenario
    tool = await configured_tool(source, scenario)
    url = f"/api/tools/{tool.id}"
    response = await scenario.client.patch(
        url,
        headers=scenario.headers,
        json={"set": {"api_key": "tenant-secret", "other": "keep", "removable": "remove"}},
    )
    assert response.status_code == 200
    before = (
        await scenario.organization.db.fetchone("SELECT value FROM tenant_settings")
        if source == "builtin"
        else await scenario.organization.db.fetchone("SELECT config AS value FROM tools")
    )
    assert before is not None
    value = json_object_from(before["value"])
    stored = json_object_from(value["config"]) if source == "builtin" else value
    ciphertext = stored["api_key"]
    response = await scenario.client.patch(
        url, headers=scenario.headers, json={"set": {"other": "changed"}, "unset": ["removable"]}
    )
    assert response.status_code == 200
    after = (
        await scenario.organization.db.fetchone("SELECT value FROM tenant_settings")
        if source == "builtin"
        else await scenario.organization.db.fetchone("SELECT config AS value FROM tools")
    )
    assert after is not None
    value = json_object_from(after["value"])
    stored = json_object_from(value["config"]) if source == "builtin" else value
    assert stored["api_key"] == ciphertext
    assert decrypt_sensitive_fields(stored) == {"api_key": "tenant-secret", "other": "changed"}
    response = await scenario.client.patch(url, headers=scenario.headers, json={"set": {"api_key": "****cret"}})
    assert response.status_code == 422


@pytest.mark.parametrize("scope", ["builtin", "admin", "agent"])
async def test_concurrent_distinct_field_updates_survive(tool_scenario: ToolScenario, scope: str) -> None:
    scenario = tool_scenario
    tool = await configured_tool("builtin" if scope == "agent" else scope, scenario)
    url = (
        f"/api/tools/agents/{scenario.agent.id}/tool-config/{tool.id}" if scope == "agent" else f"/api/tools/{tool.id}"
    )
    statuses: list[int] = []

    async def edit(key: str) -> None:
        response = await scenario.client.patch(url, headers=scenario.headers, json={"set": {key: key}})
        statuses.append(response.status_code)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(edit, "first")
        tasks.start_soon(edit, "second")
    assert statuses == [200, 200]
    if scope == "agent":
        assignment = await agent_tool_dao.get_assignment(scenario.agent.id, tool.id)
        assert assignment is not None
        assert assignment.config == {"first": "first", "second": "second"}
        assert await scenario.organization.db.fetchval("SELECT count(*) FROM agent_tools") == 1
    elif scope == "builtin":
        row = await scenario.organization.db.fetchone("SELECT value FROM tenant_settings")
        assert row is not None
        assert json_object_from(row["value"])["config"] == {"first": "first", "second": "second"}
    else:
        stored = await tool_dao.get(tool.id)
        assert stored is not None
        assert stored.config["first"] == "first"
        assert stored.config["second"] == "second"


async def test_saved_probe_resolves_clear_inheritance_without_persisting(
    tool_scenario: ToolScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tool_scenario
    tool = await configured_tool("admin", scenario)
    assignment = await agent_tool_dao.create(
        obj_in={
            "agent_id": scenario.agent.id,
            "tool_id": tool.id,
            "config": encrypt_sensitive_fields({"api_key": "agent-secret", "other": "agent"}),
        }
    )
    seen: list[str | None] = []

    async def remote(client: MCPClient) -> list[JsonObject]:
        assert get_connection() is None
        seen.append(client.api_key)
        return []

    monkeypatch.setattr(MCPClient, "list_tools", remote)
    monkeypatch.setattr("app.core.outbound_policy.validate_public_url", AsyncMock(side_effect=lambda url: url))
    ref = {"tool_id": str(tool.id), "agent_id": str(scenario.agent.id)}
    response = await scenario.client.post("/api/tools/test-mcp", headers=scenario.headers, json={"saved": ref})
    assert response.status_code == 200
    assert seen == ["agent-secret"]
    response = await scenario.client.post(
        "/api/tools/test-mcp",
        headers=scenario.headers,
        json={"saved": ref, "operations": {"unset": ["api_key"], "set": {"other": "draft"}}},
    )
    assert response.status_code == 200
    assert seen == ["agent-secret", "original-secret"]
    stored = await agent_tool_dao.get_assignment(scenario.agent.id, tool.id)
    assert stored is not None
    assert stored.config == assignment.config
    response = await scenario.client.patch(
        f"/api/tools/agents/{scenario.agent.id}/tool-config/{tool.id}",
        headers=scenario.headers,
        json={"unset": ["api_key"]},
    )
    assert response.status_code == 200
    from app.services.agent_tools import _get_tool_config

    effective = await _get_tool_config(scenario.agent.id, tool.name)
    assert effective is not None
    assert effective["api_key"] == "original-secret"
    assert effective["other"] == "agent"


async def test_agent_config_reads_mask_both_layers_and_require_access(tool_scenario: ToolScenario) -> None:
    scenario = tool_scenario
    tool = await configured_tool("admin", scenario)
    await agent_tool_dao.create(
        obj_in={
            "agent_id": scenario.agent.id,
            "tool_id": tool.id,
            "config": encrypt_sensitive_fields({"api_key": "agent-secret"}),
        }
    )
    for path in [
        f"/api/tools/agents/{scenario.agent.id}/tool-config/{tool.id}",
        f"/api/tools/agents/{scenario.agent.id}/with-config",
    ]:
        response = await scenario.client.get(path, headers=scenario.headers)
        assert response.status_code == 200
        assert "original-secret" not in response.text
        assert "agent-secret" not in response.text
        denied = await scenario.client.get(path, headers=scenario.organization.headers(scenario.organization.member))
        assert denied.status_code == 403


async def test_member_cannot_remove_network_policy_or_edit_company_config(tool_scenario: ToolScenario) -> None:
    scenario = tool_scenario
    tool = await configured_tool("builtin", scenario)
    await agent_tool_dao.create(
        obj_in={"agent_id": scenario.agent.id, "tool_id": tool.id, "config": {"allow_network": True}}
    )
    await scenario.organization.db.execute(
        "UPDATE agents SET creator_id=%(id)s WHERE id=%(agent)s",
        {"id": scenario.organization.member.id, "agent": scenario.agent.id},
    )
    headers = scenario.organization.headers(scenario.organization.member)
    response = await scenario.client.patch(
        f"/api/tools/agents/{scenario.agent.id}/tool-config/{tool.id}",
        headers=headers,
        json={"unset": ["allow_network"]},
    )
    assert response.status_code == 403
    response = await scenario.client.put(
        f"/api/tools/agents/{scenario.agent.id}/tool-config/{tool.id}",
        headers=headers,
        json={"config": {"other": "replacement"}},
    )
    assert response.status_code == 403
    response = await scenario.client.patch(f"/api/tools/{tool.id}", headers=headers, json={"set": {"other": "no"}})
    assert response.status_code == 403


async def test_saved_email_probe_uses_secret_and_redacts_provider_errors(
    tool_scenario: ToolScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tool_scenario
    tool = await tool_dao.create(obj_in={"name": "email", "source": "builtin", "config": {}})
    await agent_tool_dao.create(
        obj_in={
            "agent_id": scenario.agent.id,
            "tool_id": tool.id,
            "config": encrypt_sensitive_fields({"auth_code": "saved-email-secret", "email_address": "old@example.com"}),
        }
    )
    seen: list[object] = []

    async def remote(config):
        assert get_connection() is None
        seen.append(dict(config))
        return {"ok": False, "imap": "provider echoed saved-email-secret", "smtp": "failed saved-email-secret"}

    monkeypatch.setattr("app.services.email_service.test_connection", remote)
    response = await scenario.client.post(
        "/api/tools/test-email",
        headers=scenario.headers,
        json={
            "saved": {"tool_id": str(tool.id), "agent_id": str(scenario.agent.id)},
            "operations": {"set": {"email_address": "draft@example.com"}},
        },
    )
    assert response.status_code == 200
    assert seen == [{"auth_code": "saved-email-secret", "email_address": "draft@example.com"}]
    assert "saved-email-secret" not in response.text
    stored = await agent_tool_dao.get_assignment(scenario.agent.id, tool.id)
    assert stored is not None
    assert decrypt_sensitive_fields(stored.config)["email_address"] == "old@example.com"


async def test_saved_probe_rechecks_actor_after_remote_work(
    tool_scenario: ToolScenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tool_scenario
    tool = await configured_tool("admin", scenario)

    async def remote(_client):
        await scenario.organization.db.execute(
            "UPDATE identities SET auth_generation=auth_generation+1 WHERE id=%(id)s",
            {"id": scenario.organization.org_genesis.identity_id},
        )
        return []

    monkeypatch.setattr(MCPClient, "list_tools", remote)
    monkeypatch.setattr("app.core.outbound_policy.validate_public_url", AsyncMock(side_effect=lambda url: url))
    response = await scenario.client.post(
        "/api/tools/test-mcp", headers=scenario.headers, json={"saved": {"tool_id": str(tool.id)}}
    )
    assert response.status_code == 401
