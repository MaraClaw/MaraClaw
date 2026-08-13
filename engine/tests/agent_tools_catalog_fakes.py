import copy
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from app.services import agent_tools as agent_tools_service
from app.services.agent_tools_definitions import AGENT_TOOLS
from app.services.tool_runtime import catalog as catalog_module

AGENT_TOOLS_BY_NAME = {tool["function"]["name"]: tool for tool in AGENT_TOOLS}


@dataclass(frozen=True, slots=True)
class DbToolSpec:
    name: str
    tool_id: uuid.UUID = field(default_factory=uuid.uuid4)
    is_default: bool = True
    category: str = "general"
    okr_agent_only: bool = False


@dataclass(frozen=True, slots=True)
class CatalogCase:
    db_tools: tuple[SimpleNamespace, ...]
    assignments: tuple[SimpleNamespace, ...] = ()
    a2a_async_enabled: bool = True
    has_feishu: bool = False
    has_any_channel: bool = False
    is_system_agent: bool = False
    os_type: str = "windows"
    db_error: RuntimeError | None = None
    agent_id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = field(default_factory=uuid.uuid4)


def db_tool(spec: DbToolSpec):
    static_tool = AGENT_TOOLS_BY_NAME.get(spec.name)
    parameters = {"type": "object", "properties": {}}
    description = f"{spec.name} description"
    if static_tool:
        parameters = copy.deepcopy(static_tool["function"].get("parameters", parameters))
        description = static_tool["function"].get("description", description)
    config = {"okr_agent_only": True} if spec.okr_agent_only else {}
    return SimpleNamespace(
        id=spec.tool_id,
        name=spec.name,
        description=description,
        parameters_schema=parameters,
        is_default=spec.is_default,
        category=spec.category,
        config=config,
    )


def assignment(tool, *, enabled=True):
    return SimpleNamespace(tool_id=tool.id, enabled=enabled)


def tool_names(tools):
    return [tool["function"]["name"] for tool in tools]


def tool_named(tools, name: str):
    return next(tool for tool in tools if tool["function"]["name"] == name)


async def run_catalog(monkeypatch, case: CatalogCase):
    async def has_feishu(_agent_id):
        return case.has_feishu

    async def has_any_channel(_agent_id):
        return case.has_any_channel

    async def get_computer_os_type(_agent_id):
        return case.os_type

    monkeypatch.setattr(agent_tools_service, "_agent_has_feishu", has_feishu)
    monkeypatch.setattr(agent_tools_service, "_agent_has_any_channel", has_any_channel)
    monkeypatch.setattr(agent_tools_service, "_get_computer_os_type", get_computer_os_type)

    async def agent_get(_id):
        if case.db_error:
            raise case.db_error
        return SimpleNamespace(tenant_id=case.tenant_id, is_system=case.is_system_agent)

    async def tenant_get(_id):
        return SimpleNamespace(a2a_async_enabled=case.a2a_async_enabled)

    async def list_for_agent(_agent_id):
        if case.db_error:
            raise case.db_error
        return list(case.assignments)

    async def list_enabled_for_agent_catalog(*, agent_tenant_id, assigned_tool_ids):
        del agent_tenant_id, assigned_tool_ids
        if case.db_error:
            raise case.db_error
        return list(case.db_tools)

    observed = SimpleNamespace(calls=[])

    async def tracked_list_enabled(*, agent_tenant_id, assigned_tool_ids):
        observed.calls.append(
            {
                "agent_tenant_id": agent_tenant_id,
                "assigned_tool_ids": list(assigned_tool_ids),
            }
        )
        return await list_enabled_for_agent_catalog(
            agent_tenant_id=agent_tenant_id,
            assigned_tool_ids=assigned_tool_ids,
        )

    monkeypatch.setattr(catalog_module, "agent_dao", SimpleNamespace(get=agent_get))
    monkeypatch.setattr(catalog_module, "tenant_dao", SimpleNamespace(get=tenant_get))
    monkeypatch.setattr(catalog_module, "agent_tool_dao", SimpleNamespace(list_for_agent=list_for_agent))
    monkeypatch.setattr(
        catalog_module,
        "tool_dao",
        SimpleNamespace(list_enabled_for_agent_catalog=tracked_list_enabled),
    )

    tools = await agent_tools_service.get_agent_tools_for_llm(case.agent_id)
    return tools, observed
