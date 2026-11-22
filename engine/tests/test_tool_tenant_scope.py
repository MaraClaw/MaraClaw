import uuid

from app.api.tools import _tool_record_visible_to_agent
from app.records.tool import AgentToolRecord, ToolRecord


def make_tool(*, source: str, tenant_id: uuid.UUID | None, tool_id: uuid.UUID | None = None) -> ToolRecord:
    return ToolRecord(
        id=tool_id or uuid.uuid4(),
        name="visibility_fixture",
        display_name="Visibility Fixture",
        source=source,
        tenant_id=tenant_id,
    )


def test_builtin_tools_are_visible_across_tenants():
    tenant_id = uuid.uuid4()
    tool = make_tool(source="builtin", tenant_id=None)

    assert _tool_record_visible_to_agent(tool, tenant_id, {}) is True


def test_admin_tools_are_visible_only_to_same_tenant():
    tenant_id = uuid.uuid4()
    foreign_tenant_id = uuid.uuid4()
    same_tenant_tool = make_tool(source="admin", tenant_id=tenant_id)
    foreign_tool = make_tool(source="admin", tenant_id=foreign_tenant_id)

    assert _tool_record_visible_to_agent(same_tenant_tool, tenant_id, {}) is True
    assert _tool_record_visible_to_agent(foreign_tool, tenant_id, {}) is False


def test_agent_installed_tools_require_explicit_assignment():
    tenant_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    installed_tool = make_tool(source="agent", tool_id=tool_id, tenant_id=uuid.uuid4())
    assignment = AgentToolRecord(id=uuid.uuid4(), agent_id=uuid.uuid4(), tool_id=tool_id)

    assert _tool_record_visible_to_agent(installed_tool, tenant_id, {}) is False
    assert _tool_record_visible_to_agent(installed_tool, tenant_id, {str(tool_id): assignment}) is True
