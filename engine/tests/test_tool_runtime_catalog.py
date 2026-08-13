import ast
import sys
import uuid
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app.services import agent_tools
from app.services.agent_tools_definitions import AGENT_TOOLS
from app.services.tool_runtime import catalog

_catalog_fakes_spec = spec_from_file_location(
    "tool_runtime_catalog_fakes",
    Path(__file__).with_name("agent_tools_catalog_fakes.py"),
)
assert _catalog_fakes_spec is not None
assert _catalog_fakes_spec.loader is not None
_catalog_fakes = module_from_spec(_catalog_fakes_spec)
sys.modules[_catalog_fakes_spec.name] = _catalog_fakes
_catalog_fakes_spec.loader.exec_module(_catalog_fakes)
CatalogCase = _catalog_fakes.CatalogCase
DbToolSpec = _catalog_fakes.DbToolSpec
assignment = _catalog_fakes.assignment
db_tool = _catalog_fakes.db_tool
run_catalog = _catalog_fakes.run_catalog
tool_named = _catalog_fakes.tool_named
tool_names = _catalog_fakes.tool_names

AGENT_TOOLS_BY_NAME = {tool["function"]["name"]: tool for tool in AGENT_TOOLS}


async def test_catalog_preserves_db_order_visibility_shape_and_disabled_core(monkeypatch):
    # Given: two visible DB tools and an explicitly disabled core assignment.
    first = db_tool(DbToolSpec("first_visible"))
    second = db_tool(DbToolSpec("second_visible"))
    disabled_write = db_tool(DbToolSpec("write_file"))

    # When: the facade builds the catalog from the fake DB.
    tools, session = await run_catalog(
        monkeypatch,
        CatalogCase(
            db_tools=(first, second, disabled_write),
            assignments=(assignment(disabled_write, enabled=False),),
        ),
    )
    names = tool_names(tools)

    # Then: DB order is kept, catalog DAO is called with tenant scope, and disabled core stays disabled.
    assert names[:2] == ["first_visible", "second_visible"]
    assert "write_file" not in names
    assert session.calls
    assert session.calls[0]["agent_tenant_id"] is not None


async def test_catalog_uses_minimal_fallback_when_no_database_tools_exist(monkeypatch):
    # Given: an empty visible DB result.
    # When: catalog assembly falls back.
    tools, _session = await run_catalog(monkeypatch, CatalogCase(db_tools=()))

    # Then: only the always-core tools are exposed.
    from app.services.agent_tools_definitions import _always_core_tools

    assert tool_names(tools) == [tool["function"]["name"] for tool in _always_core_tools]


async def test_catalog_applies_channel_gates_and_a2a_schema_stripping(monkeypatch):
    # Given: a Feishu tool and an A2A tool with no configured external channel or async A2A.
    feishu_tool = db_tool(DbToolSpec("send_feishu_message", category="feishu"))
    message_tool = db_tool(DbToolSpec("send_message_to_agent"))

    # When: the facade builds the catalog.
    tools, _session = await run_catalog(
        monkeypatch,
        CatalogCase(
            db_tools=(feishu_tool, message_tool),
            has_feishu=False,
            has_any_channel=False,
            a2a_async_enabled=False,
        ),
    )

    # Then: channel-only tools are gated and the synchronous A2A schema omits msg_type.
    assert "send_feishu_message" not in tool_names(tools)
    assert "send_channel_message" not in tool_names(tools)
    message_parameters = tool_named(tools, "send_message_to_agent")["function"]["parameters"]
    assert "msg_type" not in message_parameters["properties"]
    assert "msg_type" not in message_parameters["required"]


async def test_catalog_patches_computer_description_without_mutating_static_catalog(monkeypatch):
    # Given: AgentBay file transfer is visible for a Linux computer.
    file_transfer = db_tool(DbToolSpec("agentbay_file_transfer"))

    # When: the facade builds the catalog.
    tools, _session = await run_catalog(monkeypatch, CatalogCase(db_tools=(file_transfer,), os_type="linux"))
    patched = tool_named(tools, "agentbay_file_transfer")

    # Then: the returned description is OS-specific while the static tool remains untouched.
    assert "COMPUTER ENVIRONMENT OS: Linux" in patched["function"]["description"]
    assert "/home/wuying/Desktop/file" in patched["function"]["parameters"]["properties"]["from_path"]["description"]
    assert (
        "/home/wuying/桌面/file"
        in AGENT_TOOLS_BY_NAME["agentbay_file_transfer"]["function"]["parameters"]["properties"]["from_path"][
            "description"
        ]
    )


async def test_facade_builds_fresh_catalog_dependencies_from_current_globals(monkeypatch):
    # Given: the runtime implementation records each facade-created dependency bundle.
    observed = []

    async def get_agent_tools(_agent_id, *, dependencies):
        observed.append(dependencies)
        return []

    async def first_feishu(_agent_id):
        return False

    async def second_feishu(_agent_id):
        return True

    monkeypatch.setattr(catalog, "get_agent_tools_for_llm", get_agent_tools)

    # When: facade callbacks are replaced between calls.
    # session_factory is a fixed DAO-era no-op (async_session removed).
    monkeypatch.setattr(agent_tools, "_agent_has_feishu", first_feishu)
    await agent_tools.get_agent_tools_for_llm(uuid.uuid4())
    monkeypatch.setattr(agent_tools, "_agent_has_feishu", second_feishu)
    await agent_tools.get_agent_tools_for_llm(uuid.uuid4())

    # Then: dependency bundles are rebuilt with the current facade callbacks.
    assert observed[0] is not observed[1]
    assert observed[0].agent_has_feishu is first_feishu
    assert observed[1].agent_has_feishu is second_feishu


def test_tool_runtime_never_imports_facade_registry_or_dispatcher():
    # Given: every new runtime module.
    runtime_dir = Path(__file__).parents[1] / "app" / "services" / "tool_runtime"
    forbidden = ("agent_tools", "registry", "dispatcher")

    # When: their import and dynamic-import syntax is inspected.
    imported_modules = []
    dynamic_modules = []
    for path in runtime_dir.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
            elif (
                isinstance(node, ast.Call)
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                dynamic_modules.append(node.args[0].value)

    # Then: runtime ownership has no facade, execution registry, or dispatcher dependency.
    assert not [module for module in imported_modules + dynamic_modules if any(name in module for name in forbidden)]
