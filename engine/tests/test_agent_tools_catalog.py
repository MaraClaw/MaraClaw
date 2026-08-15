import sys
import uuid
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app.services.agent_tools_definitions import (
    _ALWAYS_INCLUDE_CORE,
    _CHANNEL_MESSAGE_TOOL_NAMES,
    _FEISHU_TOOL_NAMES,
    AGENT_TOOLS,
    _always_core_tools,
    _channel_tools,
    _feishu_tools,
)
from app.services.llm.finish import FINISH_TOOL_DEFINITION, FINISH_TOOL_DEFINITION as CANONICAL_FINISH_TOOL_DEFINITION

_catalog_fakes_spec = spec_from_file_location(
    "agent_tools_catalog_fakes",
    Path(__file__).with_name("agent_tools_catalog_fakes.py"),
)
assert _catalog_fakes_spec is not None
assert _catalog_fakes_spec.loader is not None
_catalog_fakes = module_from_spec(_catalog_fakes_spec)
sys.modules[_catalog_fakes_spec.name] = _catalog_fakes
_catalog_fakes_spec.loader.exec_module(_catalog_fakes)
AGENT_TOOLS_BY_NAME = _catalog_fakes.AGENT_TOOLS_BY_NAME
CatalogCase = _catalog_fakes.CatalogCase
DbToolSpec = _catalog_fakes.DbToolSpec
assignment = _catalog_fakes.assignment
db_tool = _catalog_fakes.db_tool
run_catalog = _catalog_fakes.run_catalog
tool_named = _catalog_fakes.tool_named
tool_names = _catalog_fakes.tool_names


def test_catalog_shape_when_imported_from_public_module():
    # Given: the public agent_tools module import surface.
    expected_tool_count = 73

    # When: callers inspect the static OpenAI function-calling catalog.
    tool_names = [tool["function"]["name"] for tool in AGENT_TOOLS]

    # Then: the catalog shape and core entries match the current public contract.
    assert len(AGENT_TOOLS) == expected_tool_count
    assert FINISH_TOOL_DEFINITION is CANONICAL_FINISH_TOOL_DEFINITION
    assert AGENT_TOOLS[0] is FINISH_TOOL_DEFINITION
    assert all(tool["type"] == "function" for tool in AGENT_TOOLS)
    assert all(tool["function"]["name"] for tool in AGENT_TOOLS)
    assert len(tool_names) == len(set(tool_names))
    assert {"finish", "list_files", "read_file", "write_file", "edit_file"} <= set(tool_names)


def test_core_subsets_cohere_with_catalog():
    # Given: public static subsets used by get_agent_tools_for_llm.
    catalog_names = {tool["function"]["name"] for tool in AGENT_TOOLS}
    configured_only_feishu_tool_names = {"bitable_create_app"}

    # When: each subset is recomputed from the public catalog.
    expected_core_tools = [tool for tool in AGENT_TOOLS if tool["function"]["name"] in _ALWAYS_INCLUDE_CORE]
    expected_feishu_tools = [tool for tool in AGENT_TOOLS if tool["function"]["name"] in _FEISHU_TOOL_NAMES]
    expected_channel_tools = [tool for tool in AGENT_TOOLS if tool["function"]["name"] in _CHANNEL_MESSAGE_TOOL_NAMES]
    core_names = {tool["function"]["name"] for tool in _always_core_tools}
    feishu_names = {tool["function"]["name"] for tool in _feishu_tools}
    channel_names = {tool["function"]["name"] for tool in _channel_tools}

    # Then: the exported subsets are order-preserving views of the same catalog.
    assert "finish" in _ALWAYS_INCLUDE_CORE
    assert catalog_names >= core_names
    assert catalog_names >= feishu_names
    assert catalog_names >= channel_names
    assert catalog_names >= _ALWAYS_INCLUDE_CORE
    assert catalog_names >= _CHANNEL_MESSAGE_TOOL_NAMES
    assert _FEISHU_TOOL_NAMES - catalog_names == configured_only_feishu_tool_names
    assert core_names <= _ALWAYS_INCLUDE_CORE
    assert feishu_names <= _FEISHU_TOOL_NAMES
    assert channel_names <= _CHANNEL_MESSAGE_TOOL_NAMES
    assert _always_core_tools == expected_core_tools
    assert _feishu_tools == expected_feishu_tools
    assert _channel_tools == expected_channel_tools


async def test_db_backed_catalog_includes_default_enabled_and_assigned_visible_tools(monkeypatch):
    # Given: DB rows that represent builtin default tools plus tenant/admin/agent tools assigned to the agent.
    tenant_id = uuid.uuid4()
    default_tool = db_tool(DbToolSpec("db_default_builtin"))
    admin_tool = db_tool(DbToolSpec("tenant_admin_assigned", is_default=False))
    assigned_tool = db_tool(DbToolSpec("assigned_agent_source", is_default=False))

    # When: get_agent_tools_for_llm loads the visible DB catalog for that tenant.
    tools, db_session = await run_catalog(
        monkeypatch,
        CatalogCase(
            db_tools=(default_tool, admin_tool, assigned_tool),
            assignments=(assignment(admin_tool), assignment(assigned_tool)),
            tenant_id=tenant_id,
        ),
    )
    names = tool_names(tools)

    # Then: default and explicitly assigned rows are present, and the catalog DAO is tenant-scoped.
    assert {"db_default_builtin", "tenant_admin_assigned", "assigned_agent_source"} <= set(names)
    assert db_session.calls
    assert db_session.calls[0]["agent_tenant_id"] == tenant_id
    assert set(db_session.calls[0]["assigned_tool_ids"]) == {admin_tool.id, assigned_tool.id}
    assert len(names) == len(set(names))


async def test_explicitly_disabled_always_core_tool_is_not_readded(monkeypatch):
    # Given: write_file is a core always tool but has an explicit disabled AgentTool row.
    default_tool = db_tool(DbToolSpec("db_default_builtin"))
    disabled_write_file = db_tool(DbToolSpec("write_file"))

    # When: DB loading succeeds with at least one enabled tool.
    tools, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(
            db_tools=(default_tool, disabled_write_file),
            assignments=(assignment(disabled_write_file, enabled=False),),
        ),
    )
    names = tool_names(tools)

    # Then: write_file stays hidden instead of being appended from _always_tools.
    assert "db_default_builtin" in names
    assert "finish" in names
    assert "write_file" not in names


async def test_db_failure_or_empty_result_returns_minimal_fallback_catalog(monkeypatch):
    # Given: DB loading can either fail or return no enabled visible tools.
    fallback_cases = (
        CatalogCase(db_tools=(), db_error=RuntimeError("catalog unavailable")),
        CatalogCase(db_tools=()),
    )

    for fallback_case in fallback_cases:
        # When: get_agent_tools_for_llm uses its fallback path.
        tools, _db_session = await run_catalog(monkeypatch, fallback_case)
        names = set(tool_names(tools))

        # Then: only the minimal always-core fallback is exposed, not the full static catalog.
        assert names == {tool["function"]["name"] for tool in _always_core_tools}
        assert len(tools) < len(AGENT_TOOLS)
        assert "agentbay_file_transfer" not in names
        assert "duckduckgo_search" not in names


async def test_a2a_msg_type_is_stripped_when_tenant_disables_async_a2a(monkeypatch):
    # Given: send_message_to_agent is visible from the DB and the tenant has async A2A disabled.
    message_tool = db_tool(DbToolSpec("send_message_to_agent"))

    # When: get_agent_tools_for_llm prepares the LLM function schema.
    tools, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(db_tools=(message_tool,), a2a_async_enabled=False),
    )
    send_message_tool = tool_named(tools, "send_message_to_agent")
    parameters = send_message_tool["function"]["parameters"]

    # Then: msg_type is removed from both properties and required while the shared static catalog remains unchanged.
    assert send_message_tool["function"]["description"] == (
        "Send a message to a digital employee colleague and receive their reply synchronously."
    )
    assert "msg_type" not in parameters["properties"]
    assert "msg_type" not in parameters["required"]
    assert "msg_type" in AGENT_TOOLS_BY_NAME["send_message_to_agent"]["function"]["parameters"]["properties"]


async def test_feishu_and_channel_tools_require_configured_channels(monkeypatch):
    # Given: a Feishu DB tool is visible and channel-only tools are available only through the always list.
    default_tool = db_tool(DbToolSpec("db_default_builtin"))
    feishu_tool = db_tool(DbToolSpec("send_feishu_message", category="feishu"))

    # When: the agent has no configured external channels.
    without_channels, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(db_tools=(default_tool, feishu_tool), has_feishu=False, has_any_channel=False),
    )

    # Then: Feishu and generic channel messaging tools are not exposed.
    assert "send_feishu_message" not in tool_names(without_channels)
    assert "send_channel_message" not in tool_names(without_channels)

    # When: the agent has configured Feishu and at least one channel.
    with_channels, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(db_tools=(default_tool, feishu_tool), has_feishu=True, has_any_channel=True),
    )

    # Then: both Feishu-specific and channel messaging tools are visible.
    assert "send_feishu_message" in tool_names(with_channels)
    assert "send_channel_message" in tool_names(with_channels)


async def test_okr_system_only_tools_are_filtered_for_regular_agents(monkeypatch):
    # Given: an OKR tool is marked as system-agent-only in its DB config.
    default_tool = db_tool(DbToolSpec("db_default_builtin"))
    okr_tool = db_tool(DbToolSpec("get_okr", okr_agent_only=True))

    # When: a regular agent loads the catalog.
    regular_tools, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(db_tools=(default_tool, okr_tool), is_system_agent=False),
    )

    # Then: the OKR-only tool is hidden from regular agents.
    assert "get_okr" not in tool_names(regular_tools)

    # When: a system agent loads the same visible rows.
    system_tools, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(db_tools=(default_tool, okr_tool), is_system_agent=True),
    )

    # Then: the OKR-only tool remains visible to system agents.
    assert "get_okr" in tool_names(system_tools)


async def test_duplicate_db_tool_names_are_skipped(monkeypatch):
    # Given: old DB dumps can contain two enabled rows with the same tool name.
    first_duplicate = db_tool(DbToolSpec("duplicate_db_tool"))
    second_duplicate = db_tool(DbToolSpec("duplicate_db_tool"))

    # When: get_agent_tools_for_llm builds OpenAI function definitions.
    tools, _db_session = await run_catalog(
        monkeypatch,
        CatalogCase(db_tools=(first_duplicate, second_duplicate)),
    )
    names = tool_names(tools)

    # Then: only one duplicate name is exposed to avoid OpenAI function-name collisions.
    assert names.count("duplicate_db_tool") == 1
    assert len(names) == len(set(names))


async def test_agentbay_file_transfer_paths_are_patched_for_configured_os(monkeypatch):
    # Given: agentbay_file_transfer is visible for both supported AgentBay computer OS configurations.
    file_transfer_tool = db_tool(DbToolSpec("agentbay_file_transfer"))
    os_cases = (
        ("windows", "Windows", r"C:\Users\Administrator\Desktop\report.xlsx", r"C:\Users\Administrator\Desktop\file"),
        ("linux", "Linux", "/home/wuying/Desktop/report.xlsx", "/home/wuying/Desktop/file"),
    )

    for os_type, label, description_path, hint_path in os_cases:
        # When: get_agent_tools_for_llm patches the function description and parameter hints.
        tools, _db_session = await run_catalog(
            monkeypatch,
            CatalogCase(db_tools=(file_transfer_tool,), os_type=os_type),
        )
        patched_tool = tool_named(tools, "agentbay_file_transfer")
        description = patched_tool["function"]["description"]
        properties = patched_tool["function"]["parameters"]["properties"]

        # Then: the configured OS path examples replace the static Linux-ish defaults.
        assert f"COMPUTER ENVIRONMENT OS: {label}" in description
        assert description_path in description
        assert hint_path in properties["from_path"]["description"]
        assert hint_path in properties["to_path"]["description"]
    assert (
        "/home/wuying/桌面/file"
        in AGENT_TOOLS_BY_NAME["agentbay_file_transfer"]["function"]["parameters"]["properties"]["from_path"][
            "description"
        ]
    )
