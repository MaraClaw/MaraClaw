from collections.abc import AsyncIterator, Mapping, Sequence
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import tools
from app.core import permissions
from app.core.domain.agents import PermissionAccessLevel
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject, json_value_from_response
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.tool import AgentToolRecord, ToolRecord
from app.records.user import UserRecord


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord]:
    user = UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN, tenant_id=uuid4())
    agent = AgentRecord(id=uuid4(), name="agent", creator_id=user.id, tenant_id=user.tenant_id)
    tool = ToolRecord(id=uuid4(), name="custom", source="admin", type="mcp", tenant_id=user.tenant_id)
    assignment = AgentToolRecord(id=uuid4(), agent_id=agent.id, tool_id=tool.id)
    app = FastAPI()
    app.include_router(tools.router, prefix="/api")
    app.dependency_overrides[tools.get_current_user] = lambda: user

    async def access(user: UserRecord, agent_id: UUID) -> tuple[AgentRecord, PermissionAccessLevel]:
        return agent, PermissionAccessLevel.MANAGE

    async def get_tool(tool_id: UUID) -> ToolRecord:
        return tool

    async def get_agent(agent_id: UUID) -> AgentRecord:
        return agent

    async def assigned(agent_id: UUID, tool_id: UUID) -> AgentToolRecord:
        return assignment

    async def assignments(agent_id: UUID) -> list[AgentToolRecord]:
        return [assignment]

    async def tool_list(**kwargs: object) -> list[ToolRecord]:
        return [tool]

    async def category_list(category: str, **kwargs: object) -> list[ToolRecord]:
        return [tool]

    async def no_channel(**kwargs: object) -> None:
        return None

    async def create_channel(*, obj_in: Mapping[str, object]) -> ChannelConfigRecord:
        return ChannelConfigRecord(id=uuid4(), agent_id=agent.id)

    async def update_assignment(*, db_obj: AgentToolRecord, obj_in: Mapping[str, object]) -> AgentToolRecord:
        return db_obj

    async def company_config(tool: ToolRecord, tenant_id: UUID | None) -> JsonObject:
        return {"api_key": "company-secret", "vendor": {"nullable": None, "flags": [True, 3]}}

    async def builtin_configs(tenant_id: UUID | None, records: Sequence[ToolRecord]) -> dict[str, JsonObject]:
        return {}

    async def catalog(tenant_id: UUID | None) -> list[ToolRecord]:
        return [tool]

    async def has_feishu(agent_id: UUID) -> bool:
        return False

    from app.services import agent_tools

    monkeypatch.setattr(tools, "check_agent_access", access)
    monkeypatch.setattr(permissions, "check_agent_access", access)
    monkeypatch.setattr(tools.tool_dao, "get", get_tool)
    monkeypatch.setattr(tools.agent_dao, "get", get_agent)
    monkeypatch.setattr(tools.agent_tool_dao, "get_assignment", assigned)
    monkeypatch.setattr(tools.agent_tool_dao, "list_for_agent", assignments)
    monkeypatch.setattr(tools.agent_tool_dao, "update", update_assignment)
    monkeypatch.setattr(tools.tool_dao, "list_enabled_visible", tool_list)
    monkeypatch.setattr(tools.tool_dao, "list_enabled_by_category_visible", category_list)
    monkeypatch.setattr(tools.tool_dao, "list_platform_for_tenant", catalog)
    monkeypatch.setattr(tools.channel_config_dao, "get_for_agent", no_channel)
    monkeypatch.setattr(tools.channel_config_dao, "create", create_channel)
    monkeypatch.setattr(tools, "get_tool_company_config", company_config)
    monkeypatch.setattr(tools, "get_tenant_tool_configs", builtin_configs)
    monkeypatch.setattr(agent_tools, "_agent_has_feishu", has_feishu)
    return app, agent, tool, assignment


@pytest.fixture
async def client(context: tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord]) -> AsyncIterator[httpx.AsyncClient]:
    app = context[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as session:
        yield session
    app.dependency_overrides.clear()


async def test_create_tool_http_returns_only_id_and_name(
    client: httpx.AsyncClient,
    context: tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a catalog persistence boundary returning a real tool record.
    tool = context[2]

    async def absent(name: str, tenant_id: UUID | None) -> None:
        return None

    async def create(*, obj_in: Mapping[str, object]) -> ToolRecord:
        return tool

    monkeypatch.setattr(tools.tool_dao, "get_by_name_and_tenant", absent)
    monkeypatch.setattr(tools.tool_dao, "create", create)
    # When creating through the actual HTTP response serializer.
    response = await client.post("/api/tools", json={"name": tool.name, "display_name": "Custom"})
    # Then the concrete contract does not leak internal tool fields.
    assert (response.status_code, response.json()) == (200, {"id": str(tool.id), "name": "custom"})


@pytest.mark.parametrize("operation", ["assignment", "config", "category"])
async def test_agent_tool_writes_http_preserve_acknowledgement(
    client: httpx.AsyncClient,
    context: tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord],
    operation: str,
) -> None:
    # Given authorized access to an agent and its assigned tool.
    _, agent, tool, _ = context
    path = f"/api/tools/agents/{agent.id}"
    # When saving each kind of agent tool state.
    match operation:
        case "assignment":
            response = await client.put(path, json=[{"tool_id": str(tool.id), "enabled": True}])
        case "config":
            response = await client.put(f"{path}/tool-config/{tool.id}", json={"config": {"vendor": [None, False]}})
        case _:
            response = await client.post(f"{path}/category-config/custom", json={"config": {"vendor": [None, False]}})
    # Then success remains the same one-field body.
    assert (response.status_code, response.json()) == (200, {"ok": True})


async def test_agent_config_http_preserves_masking_and_open_provider_values(
    client: httpx.AsyncClient,
    context: tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord],
) -> None:
    # Given different company and agent secrets plus provider-owned nested data.
    _, agent, tool, assignment = context
    assignment.config = tools._encrypt_sensitive_fields(
        {"api_key": "agent-secret", "vendor": {"override": [None, False]}}
    )
    # When reading merged per-agent tool configuration.
    response = await client.get(f"/api/tools/agents/{agent.id}/tool-config/{tool.id}")
    # Then the existing masking policy and arbitrary JSON values survive validation.
    assert response.status_code == 200
    assert response.json() == {
        "global_config": {"api_key": "****cret", "vendor": {"nullable": None, "flags": [True, 3]}},
        "agent_config": {"api_key": "****cret", "vendor": {"override": [None, False]}},
        "merged_config": {"api_key": "****cret", "vendor": {"override": [None, False]}},
        "config_schema": {},
    }


@pytest.mark.parametrize("path", ["/api/tools", "agent-with-config", "category"])
async def test_tool_read_http_preserves_nullable_fields_and_open_configs(
    client: httpx.AsyncClient,
    context: tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord],
    path: str,
) -> None:
    # Given a tool with absent MCP metadata and provider-defined company configuration.
    _, agent, _, _ = context
    paths = {
        "agent-with-config": f"/api/tools/agents/{agent.id}/with-config",
        "category": f"/api/tools/agents/{agent.id}/category-config/custom",
    }
    # When reading each concrete response envelope.
    response = await client.get(paths.get(path, path))
    # Then optional values stay explicit nulls and nested config data is retained.
    assert response.status_code == 200
    body = json_value_from_response(response)
    if isinstance(body, list):
        body = body[0]
    assert isinstance(body, dict)
    field = "config" if path == "/api/tools" else "global_config"
    assert body[field] == {"api_key": "****cret", "vendor": {"nullable": None, "flags": [True, 3]}}
    if path == "category":
        assert body["id"] is None
        assert body["agent_config"] == {}
    else:
        assert body["mcp_server_name"] is None
        assert body["mcp_server_url"] is None


async def test_category_delete_http_remains_bodyless(
    client: httpx.AsyncClient,
    context: tuple[FastAPI, AgentRecord, ToolRecord, AgentToolRecord],
) -> None:
    # Given an authorized creator, even when no category configuration exists.
    agent = context[1]
    # When deleting the configuration.
    response = await client.delete(f"/api/tools/agents/{agent.id}/category-config/custom")
    # Then success has neither JSON null nor an advertised JSON content type.
    assert (response.status_code, response.content) == (204, b"")
    assert "content-type" not in response.headers
