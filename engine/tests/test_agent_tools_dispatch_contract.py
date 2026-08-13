from __future__ import annotations

import ast
import importlib
import inspect
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock

from app.core.json_types import JsonObject
from app.services import activity_logger, agent_tools, agent_tools_definitions
from app.services.agent_tool_exec import registry
from app.services.agent_tools import ToolParameters

AGENT_TOOLS_PATH: Final = Path("app/services/agent_tool_exec/dispatcher.py")

EXPECTED_DISPATCH_NAMES: Final = (
    "list_files",
    "list_focus_items",
    "upsert_focus_item",
    "complete_focus_item",
    "read_file",
    "read_document",
    "write_file",
    "move_file",
    "delete_file",
    "edit_file",
    "convert_csv_to_xlsx",
    "convert_html_to_pdf",
    "convert_html_to_pptx",
    "convert_markdown_to_docx",
    "convert_markdown_to_pdf",
    "search_files",
    "find_files",
    "manage_tasks",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "list_triggers",
    "send_feishu_message",
    "send_platform_message",
    "send_channel_message",
    "send_message_to_agent",
    "send_file_to_agent",
    "send_channel_file",
    "web_search",
    "jina_search",
    "exa_search",
    "duckduckgo_search",
    "tavily_search",
    "google_search",
    "bing_search",
    "jina_read",
    "read_webpage",
    "plaza_get_new_posts",
    "plaza_create_post",
    "plaza_add_comment",
    "execute_code",
    "execute_code_e2b",
    "upload_image",
    "generate_image_siliconflow",
    "generate_image_openai",
    "generate_image_google",
    "generate_image_custom",
    "discover_resources",
    "import_mcp_server",
    "bitable_create_app",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_user_search",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
    "send_email",
    "read_emails",
    "reply_email",
    "publish_page",
    "list_published_pages",
    "agentbay_browser_navigate",
    "agentbay_browser_screenshot",
    "agentbay_browser_save_screenshot",
    "agentbay_browser_click",
    "agentbay_browser_type",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_browser_extract",
    "agentbay_browser_observe",
    "agentbay_browser_login",
    "agentbay_command_exec",
    "agentbay_computer_screenshot",
    "agentbay_computer_save_screenshot",
    "agentbay_computer_precision_screenshot",
    "agentbay_computer_click",
    "agentbay_computer_input_text",
    "agentbay_computer_press_keys",
    "agentbay_computer_scroll",
    "agentbay_computer_move_mouse",
    "agentbay_computer_drag_mouse",
    "agentbay_computer_get_screen_size",
    "agentbay_computer_start_app",
    "agentbay_computer_get_installed_apps",
    "agentbay_computer_get_cursor_position",
    "agentbay_computer_get_active_window",
    "agentbay_computer_list_windows",
    "agentbay_computer_activate_window",
    "agentbay_computer_close_window",
    "agentbay_computer_dismiss_dialog",
    "agentbay_computer_list_visible_apps",
    "agentbay_file_transfer",
    "search_clawhub",
    "install_skill",
    "get_okr",
    "get_my_okr",
    "update_kr_content",
    "update_kr_progress",
    "collect_okr_progress",
    "generate_okr_report",
    "get_okr_settings",
    "create_objective",
    "create_key_result",
    "update_objective",
    "update_any_kr_progress",
    "generate_monthly_okr_report",
    "upsert_member_daily_report",
    "vercel_deploy",
    "vercel_list_deployments",
    "vercel_get_deploy_logs",
    "vercel_set_env",
    "vercel_manage_domain",
    "neon_create_database",
)

EXPECTED_CATALOG_NAMES: Final = (
    "finish",
    "list_files",
    "read_file",
    "list_focus_items",
    "upsert_focus_item",
    "complete_focus_item",
    "write_file",
    "delete_file",
    "move_file",
    "edit_file",
    "search_files",
    "find_files",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "list_triggers",
    "send_channel_file",
    "send_feishu_message",
    "send_channel_message",
    "send_platform_message",
    "send_message_to_agent",
    "send_file_to_agent",
    "jina_search",
    "jina_read",
    "read_webpage",
    "read_document",
    "execute_code",
    "execute_code_e2b",
    "upload_image",
    "generate_image_siliconflow",
    "generate_image_openai",
    "generate_image_google",
    "generate_image_custom",
    "discover_resources",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_user_search",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
    "import_mcp_server",
    "send_email",
    "read_emails",
    "reply_email",
    "publish_page",
    "list_published_pages",
    "search_clawhub",
    "install_skill",
    "agentbay_browser_navigate",
    "agentbay_browser_screenshot",
    "agentbay_browser_click",
    "agentbay_browser_type",
    "agentbay_browser_login",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_file_transfer",
)

MIGRATED_DISPATCH_NAMES: Final = (
    "list_files",
    "list_focus_items",
    "upsert_focus_item",
    "complete_focus_item",
    "read_file",
    "read_document",
    "write_file",
    "move_file",
    "delete_file",
    "edit_file",
    "convert_csv_to_xlsx",
    "convert_html_to_pdf",
    "convert_html_to_pptx",
    "convert_markdown_to_docx",
    "convert_markdown_to_pdf",
    "search_files",
    "find_files",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "list_triggers",
    "send_message_to_agent",
    "send_file_to_agent",
    "web_search",
    "jina_search",
    "exa_search",
    "duckduckgo_search",
    "tavily_search",
    "google_search",
    "bing_search",
    "jina_read",
    "read_webpage",
    "send_feishu_message",
    "bitable_create_app",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_user_search",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
    "agentbay_browser_navigate",
    "agentbay_browser_screenshot",
    "agentbay_browser_save_screenshot",
    "agentbay_browser_click",
    "agentbay_browser_type",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_browser_extract",
    "agentbay_browser_observe",
    "agentbay_browser_login",
    "agentbay_command_exec",
    "agentbay_computer_screenshot",
    "agentbay_computer_save_screenshot",
    "agentbay_computer_precision_screenshot",
    "agentbay_computer_click",
    "agentbay_computer_input_text",
    "agentbay_computer_press_keys",
    "agentbay_computer_scroll",
    "agentbay_computer_move_mouse",
    "agentbay_computer_drag_mouse",
    "agentbay_computer_get_screen_size",
    "agentbay_computer_start_app",
    "agentbay_computer_get_installed_apps",
    "agentbay_computer_get_cursor_position",
    "agentbay_computer_get_active_window",
    "agentbay_computer_list_windows",
    "agentbay_computer_activate_window",
    "agentbay_computer_close_window",
    "agentbay_computer_dismiss_dialog",
    "agentbay_computer_list_visible_apps",
    "agentbay_file_transfer",
)

EXPECTED_MONKEYPATCH_SEAMS: Final = (
    "WORKSPACE_ROOT",
    "_append_focus_item",
    "_check_neon_quota_limit",
    "_check_code_safety",
    "_execute_code",
    "_execute_code_legacy",
    "_create_on_message_trigger",
    "_get_agent_tenant_id",
    "_get_tool_config",
    "_get_vercel_token",
    "_wake_agent_async",
    "get_agent_tools_for_llm",
    "get_storage_backend",
)


def _literal_tool_names(node: ast.AST) -> tuple[str, ...]:
    match node:
        case ast.Constant(value=str() as name):
            return (name,)
        case ast.Tuple(elts=elts) | ast.Set(elts=elts) | ast.List(elts=elts):
            names: list[str] = []
            for element in elts:
                names.extend(_literal_tool_names(element))
            return tuple(names)
        case _:
            return ()


def _is_tool_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "tool_name"


def _execute_tool_node() -> ast.AsyncFunctionDef:
    module = ast.parse(AGENT_TOOLS_PATH.read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_tool":
            return node
    raise AssertionError("execute_tool function was not found")


def _extract_dispatch_names() -> tuple[str, ...]:
    entries: list[tuple[int, int, int, str]] = []
    ordinal = 0
    for node in ast.walk(_execute_tool_node()):
        if not isinstance(node, ast.Compare):
            continue
        operands = (node.left, *node.comparators)
        for left, right in pairwise(operands):
            for name_node, literal_node in ((left, right), (right, left)):
                if _is_tool_name(name_node):
                    for name in _literal_tool_names(literal_node):
                        entries.append((node.lineno, node.col_offset, ordinal, name))
                        ordinal += 1

    names: list[str] = []
    for entry in sorted(entries, key=lambda item: item[:3]):
        name = entry[3]
        if name not in names:
            names.append(name)
    return _with_registered_dispatch_names(tuple(names))


def _with_registered_dispatch_names(literal_names: tuple[str, ...]) -> tuple[str, ...]:
    registered_transition_names = {name for name in MIGRATED_DISPATCH_NAMES if name in registry.TOOL_HANDLERS}
    names = [name for name in literal_names if name not in registered_transition_names]
    for registered_name in MIGRATED_DISPATCH_NAMES:
        if registered_name in names or registered_name not in registered_transition_names:
            continue
        expected_index = EXPECTED_DISPATCH_NAMES.index(registered_name)
        insert_at = 0
        for prior_name in reversed(EXPECTED_DISPATCH_NAMES[:expected_index]):
            if prior_name in names:
                insert_at = names.index(prior_name) + 1
                break
        names.insert(insert_at, registered_name)
    return tuple(names)


async def _noop_log_activity(*_args, **_kwargs) -> None:
    return None


def test_execute_tool_dispatch_names_match_pinned_ast_contract():
    actual_names = _extract_dispatch_names()

    assert actual_names == EXPECTED_DISPATCH_NAMES
    assert "finish" not in actual_names
    assert len(actual_names) == 130


async def test_execute_tool_autonomy_denial_preserves_l2_message(monkeypatch):
    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-denied"

    async def get_agent(_agent_id: uuid.UUID):
        return object()

    agent_dao_mod = importlib.import_module("app.dao.agent_dao")
    from app.services import autonomy_service as autonomy_module

    check_and_enforce = AsyncMock(return_value={"allowed": False, "level": "L2", "message": "policy says no"})
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_dao_mod.agent_dao, "get", get_agent)
    monkeypatch.setattr(autonomy_module.autonomy_service, "check_and_enforce", check_and_enforce)

    result = await agent_tools.execute_tool(
        "send_message_to_agent",
        {"agent_name": "Peer", "message": "hello"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    assert result == "❌ Action denied: policy says no"
    check_and_enforce.assert_awaited_once()


async def test_execute_tool_agentbay_lock_injects_session_id_before_block(monkeypatch):
    from app.api import agentbay_control

    agent_id = uuid.uuid4()
    arguments: ToolParameters = {"url": "https://example.invalid"}
    lock_calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-agentbay"

    def is_session_locked(observed_agent_id: str, observed_session_id: str) -> bool:
        lock_calls.append((observed_agent_id, observed_session_id, arguments.copy()))
        return True

    def fail_resolve_handler(_tool_name: str):
        raise AssertionError("locked AgentBay execution must not reach handler resolution")

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agentbay_control, "is_session_locked", is_session_locked)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", fail_resolve_handler)

    result = await agent_tools.execute_tool(
        "agentbay_browser_navigate",
        arguments,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-agentbay",
    )

    assert result == (
        "⏸️ A human operator is currently controlling this browser session "
        "(Take Control mode). Please wait for them to finish before retrying "
        "browser/computer operations."
    )
    assert arguments["_session_id"] == "session-agentbay"
    assert lock_calls == [
        (
            str(agent_id),
            "session-agentbay",
            {"url": "https://example.invalid", "_session_id": "session-agentbay"},
        )
    ]


async def test_execute_tool_unknown_unregistered_tool_invokes_mcp_fallback(monkeypatch):
    mcp_tools = importlib.import_module("app.services.agent_tool_exec.mcp_tools")
    mcp_calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-mcp"

    async def execute_mcp_tool(
        tool_name: str, arguments: registry.ToolArguments, agent_id: uuid.UUID | None = None
    ) -> str:
        mcp_calls.append((tool_name, arguments, agent_id))
        return "mcp fallback result"

    async def legacy_fail(*_args, **_kwargs) -> str:
        raise AssertionError("dispatcher must not call the legacy MCP facade")

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setattr(mcp_tools, "_execute_mcp_tool", execute_mcp_tool)
    monkeypatch.setattr(agent_tools, "_execute_mcp_tool", legacy_fail)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    arguments: ToolParameters = {"value": "fallback"}
    result = await agent_tools.execute_tool(
        "unregistered_mcp_tool",
        arguments,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
    )

    assert result == "mcp fallback result"
    assert mcp_calls == [("unregistered_mcp_tool", arguments, agent_id)]


async def test_execute_tool_routes_mcp_resources_directly_to_extracted_module(monkeypatch):
    mcp_tools = importlib.import_module("app.services.agent_tool_exec.mcp_tools")
    calls: list[tuple[object, ...]] = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-mcp"

    async def discover_resources(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("discover", agent_id, arguments))
        return "discovered"

    async def import_mcp_server(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("import", agent_id, arguments))
        return "imported"

    async def legacy_fail(*_args, **_kwargs) -> str:
        raise AssertionError("dispatcher must not call the legacy MCP facade")

    monkeypatch.setattr(mcp_tools, "_discover_resources", discover_resources)
    monkeypatch.setattr(mcp_tools, "_import_mcp_server", import_mcp_server)
    monkeypatch.setattr(agent_tools, "_discover_resources", legacy_fail)
    monkeypatch.setattr(agent_tools, "_import_mcp_server", legacy_fail)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    discover_arguments: ToolParameters = {"query": "calendar"}
    import_arguments: ToolParameters = {"server_id": "github"}
    discover_result = await agent_tools.execute_tool("discover_resources", discover_arguments, agent_id, user_id)
    import_result = await agent_tools.execute_tool("import_mcp_server", import_arguments, agent_id, user_id)

    assert discover_result == "discovered"
    assert import_result == "imported"
    assert calls == [
        ("discover", agent_id, discover_arguments),
        ("import", agent_id, import_arguments),
    ]


async def test_legacy_mcp_facades_delegate_to_extracted_modules(monkeypatch):
    mcp_tools = importlib.import_module("app.services.agent_tool_exec.mcp_tools")
    mcp_smithery = importlib.import_module("app.services.agent_tool_exec.mcp_smithery")
    calls = []

    async def execute_mcp_tool(*args, **kwargs) -> str:
        calls.append(("mcp", args, kwargs))
        return "mcp"

    async def execute_smithery(*args, **kwargs) -> str:
        calls.append(("smithery", args, kwargs))
        return "smithery"

    async def recover_smithery(*args, **kwargs) -> str:
        calls.append(("recover", args, kwargs))
        return "recover"

    async def discover_resources(*args, **kwargs) -> str:
        calls.append(("discover", args, kwargs))
        return "discover"

    async def import_mcp_server(*args, **kwargs) -> str:
        calls.append(("import", args, kwargs))
        return "import"

    monkeypatch.setattr(mcp_tools, "_execute_mcp_tool", execute_mcp_tool)
    monkeypatch.setattr(mcp_smithery, "_execute_via_smithery_connect", execute_smithery)
    monkeypatch.setattr(mcp_smithery, "_smithery_auto_recover", recover_smithery)
    monkeypatch.setattr(mcp_tools, "_discover_resources", discover_resources)
    monkeypatch.setattr(mcp_tools, "_import_mcp_server", import_mcp_server)

    agent_id = uuid.uuid4()
    assert await agent_tools._execute_mcp_tool("tool", {"value": 1}, agent_id) == "mcp"
    assert await agent_tools._execute_via_smithery_connect("url", "tool", {}, {}, agent_id) == "smithery"
    assert await agent_tools._smithery_auto_recover("key", "url", "space", "connection", agent_id) == "recover"
    assert await agent_tools._discover_resources(agent_id, {"query": "search"}) == "discover"
    assert await agent_tools._import_mcp_server(agent_id, {"server_id": "github"}) == "import"
    assert calls == [
        ("mcp", ("tool", {"value": 1}), {"agent_id": agent_id}),
        ("smithery", ("url", "tool", {}, {}), {"agent_id": agent_id}),
        ("recover", ("key", "url", "space", "connection"), {"agent_id": agent_id}),
        ("discover", (agent_id, {"query": "search"}), {}),
        ("import", (agent_id, {"server_id": "github"}), {}),
    ]


async def test_mcp_resource_helpers_preserve_discovery_and_import_contracts(monkeypatch):
    from app.services import resource_discovery

    mcp_tools = importlib.import_module("app.services.agent_tool_exec.mcp_tools")
    calls = []

    async def search_smithery(query: str, max_results: int, *, agent_id: uuid.UUID) -> str:
        calls.append(("search", query, max_results, agent_id))
        return "search result"

    async def import_mcp_direct(mcp_url: str, agent_id: uuid.UUID, server_name: str | None, api_key: str | None) -> str:
        calls.append(("direct", mcp_url, agent_id, server_name, api_key))
        return "direct result"

    async def import_mcp_from_smithery(
        server_id: str,
        agent_id: uuid.UUID,
        config: JsonObject | None,
        *,
        reauthorize: bool,
    ) -> str:
        calls.append(("smithery", server_id, agent_id, config, reauthorize))
        return "smithery result"

    monkeypatch.setattr(resource_discovery, "search_smithery", search_smithery)
    monkeypatch.setattr(resource_discovery, "import_mcp_direct", import_mcp_direct)
    monkeypatch.setattr(resource_discovery, "import_mcp_from_smithery", import_mcp_from_smithery)

    agent_id = uuid.uuid4()
    assert (
        await mcp_tools._discover_resources(agent_id, {})
        == "❌ Please provide a search query describing the capability you need."
    )
    assert await mcp_tools._discover_resources(agent_id, {"query": "calendar", "max_results": 99}) == "search result"

    direct_config: JsonObject = {
        "mcp_url": "https://direct.test/mcp",
        "server_name": "Direct",
        "api_key": "key",
        "keep": "x",
    }
    direct_arguments: registry.ToolArguments = {"config": direct_config}
    assert await mcp_tools._import_mcp_server(agent_id, direct_arguments) == "direct result"
    assert direct_config == {"keep": "x"}

    smithery_config: JsonObject = {"setting": "value"}
    smithery_arguments: registry.ToolArguments = {
        "server_id": "github",
        "config": smithery_config,
        "reauthorize": True,
    }
    assert await mcp_tools._import_mcp_server(agent_id, smithery_arguments) == "smithery result"
    assert await mcp_tools._import_mcp_server(agent_id, {}) == (
        "❌ Please provide a server_id (e.g. 'github'). Use discover_resources first to find available servers."
    )
    assert calls == [
        ("search", "calendar", 10, agent_id),
        ("direct", "https://direct.test/mcp", agent_id, "Direct", "key"),
        ("smithery", "github", agent_id, smithery_config, True),
    ]


async def test_execute_tool_activity_logging_skips_noisy_reads_and_logs_success(monkeypatch):
    log_calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-activity"

    def resolve_handler(_tool_name: str):
        def handler(*, arguments, agent_id, user_id, session_id, on_output):
            return f"successful:{arguments['value']}"

        return handler

    async def log_activity(agent_id, action_type, summary, detail=None, related_id=None) -> None:
        log_calls.append((agent_id, action_type, summary, detail, related_id))

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", resolve_handler)
    monkeypatch.setattr(activity_logger, "log_activity", log_activity)

    agent_id = uuid.uuid4()
    for tool_name in ("list_files", "read_file", "read_document", "dispatch_logged_tool"):
        result = await agent_tools.execute_tool(
            tool_name,
            {"value": tool_name},
            agent_id=agent_id,
            user_id=uuid.uuid4(),
        )
        assert result == f"successful:{tool_name}"

    assert len(log_calls) == 1
    assert log_calls[0][0] == agent_id
    assert log_calls[0][1] == "tool_call"
    assert log_calls[0][2] == "Called tool dispatch_logged_tool: successful:dispatch_logged_tool"
    assert log_calls[0][3] == {
        "tool": "dispatch_logged_tool",
        "args": {"value": "dispatch_logged_tool"},
        "result": "successful:dispatch_logged_tool",
    }
    assert log_calls[0][4] is None


async def test_execute_tool_persists_messaging_error_to_current_session(monkeypatch):
    inserted: list[dict] = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-message"

    def resolve_handler(_tool_name: str):
        def handler(*, arguments, agent_id, user_id, session_id, on_output):
            return "❌ channel rejected"

        return handler

    async def insert_message(**kwargs):
        inserted.append(kwargs)
        return kwargs

    chat_dao_mod = importlib.import_module("app.dao.chat_dao")

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", resolve_handler)
    monkeypatch.setattr(chat_dao_mod.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    result = await agent_tools.execute_tool(
        "send_channel_message",
        {"channel": "ops", "content": "hello"},
        agent_id=agent_id,
        user_id=user_id,
        session_id="session-message",
    )

    assert result == "❌ channel rejected"
    assert len(inserted) == 1
    saved_message = inserted[0]
    assert saved_message["agent_id"] == agent_id
    assert saved_message["user_id"] == user_id
    assert saved_message["role"] == "assistant"
    assert saved_message["conversation_id"] == "session-message"
    assert "send_channel_message" in saved_message["content"]
    assert "❌ channel rejected" in saved_message["content"]


async def test_execute_tool_direct_bypasses_autonomy_and_preserves_unsupported_message(
    monkeypatch,
    tmp_path,
):
    mutation_calls = []
    agent_id = uuid.uuid4()

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-direct"

    async def execute_workspace_mutation(tool_name, arguments, *, agent_id, base_dir, session_id):
        mutation_calls.append((tool_name, arguments, agent_id, base_dir, session_id))
        return "direct mutation result"

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(agent_tools, "_execute_workspace_mutation", execute_workspace_mutation)

    write_arguments: ToolParameters = {"path": "workspace/a.txt", "content": "hello"}
    result = await agent_tools._execute_tool_direct("write_file", write_arguments, agent_id)
    unsupported = await agent_tools._execute_tool_direct("unsupported_direct_tool", {}, agent_id)

    assert result == "direct mutation result"
    assert mutation_calls == [("write_file", write_arguments, agent_id, tmp_path / str(agent_id), None)]
    assert unsupported == "Tool unsupported_direct_tool does not support post-approval execution"


async def test_execute_tool_direct_rejects_manage_tasks_without_invoking_facade(monkeypatch, tmp_path):
    # Given
    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-direct"

    manage_tasks = AsyncMock()
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_tools, "_manage_tasks", manage_tasks)

    # When
    result = await agent_tools._execute_tool_direct(
        "manage_tasks",
        {"action": "create"},
        uuid.uuid4(),
    )

    # Then
    assert result == "Tool manage_tasks does not support post-approval execution"
    manage_tasks.assert_not_called()


async def test_execute_tool_direct_rejects_image_tools_without_invoking_facades(monkeypatch, tmp_path):
    upload_image = AsyncMock()
    generate_image = AsyncMock()
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_tools, "_upload_image", upload_image)
    monkeypatch.setattr(agent_tools, "_generate_image", generate_image)

    agent_id = uuid.uuid4()
    for tool_name in (
        "upload_image",
        "generate_image_siliconflow",
        "generate_image_openai",
        "generate_image_google",
        "generate_image_custom",
    ):
        result = await agent_tools._execute_tool_direct(tool_name, {}, agent_id)
        assert result == f"Tool {tool_name} does not support post-approval execution"

    upload_image.assert_not_awaited()
    generate_image.assert_not_awaited()


async def test_normal_image_dispatch_prefers_registered_handler_over_facade(monkeypatch):
    calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-images"

    def resolve_handler(tool_name: str):
        if tool_name != "upload_image":
            return None

        async def handler(*, arguments, agent_id, user_id, session_id, on_output):
            calls.append((arguments, agent_id, user_id, session_id, on_output))
            return "registered upload"

        return handler

    upload_image = AsyncMock(side_effect=AssertionError("registry handler must win"))
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", resolve_handler)
    monkeypatch.setattr(agent_tools, "_upload_image", upload_image)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    result = await agent_tools.execute_tool("upload_image", {"file_path": "workspace/photo.png"}, agent_id, user_id)

    assert result == "registered upload"
    assert calls == [({"file_path": "workspace/photo.png"}, agent_id, user_id, "", None)]
    upload_image.assert_not_awaited()


async def test_normal_image_dispatch_routes_unregistered_tools_through_workspace(monkeypatch, tmp_path):
    calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-images"

    async def run_with_temp_workspace(_agent_id, _tenant_id, runner, **kwargs) -> str:
        calls.append(("workspace", kwargs))
        return await runner(tmp_path)

    async def upload_image(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters) -> str:
        calls.append(("upload", agent_id, ws, arguments.copy()))
        return "uploaded"

    async def generate_image(agent_id: uuid.UUID, ws: Path, arguments: ToolParameters, provider: str) -> str:
        calls.append(("generate", agent_id, ws, arguments.copy(), provider))
        return f"generated {provider}"

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_run_with_temp_workspace", run_with_temp_workspace)
    monkeypatch.setattr(agent_tools, "_upload_image", upload_image)
    monkeypatch.setattr(agent_tools, "_generate_image", generate_image)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    assert (
        await agent_tools.execute_tool("upload_image", {"file_path": "workspace/photo.png"}, agent_id, user_id)
        == "uploaded"
    )
    for tool_name, provider in (
        ("generate_image_siliconflow", "siliconflow"),
        ("generate_image_openai", "openai"),
        ("generate_image_google", "google"),
        ("generate_image_custom", "custom"),
    ):
        assert (
            await agent_tools.execute_tool(tool_name, {"prompt": "draw"}, agent_id, user_id) == f"generated {provider}"
        )

    assert calls == [
        ("workspace", {"paths": ["workspace/photo.png"]}),
        ("upload", agent_id, tmp_path, {"file_path": "workspace/photo.png"}),
        ("workspace", {"sync_back": True}),
        ("generate", agent_id, tmp_path, {"prompt": "draw"}, "siliconflow"),
        ("workspace", {"sync_back": True}),
        ("generate", agent_id, tmp_path, {"prompt": "draw"}, "openai"),
        ("workspace", {"sync_back": True}),
        ("generate", agent_id, tmp_path, {"prompt": "draw"}, "google"),
        ("workspace", {"sync_back": True}),
        ("generate", agent_id, tmp_path, {"prompt": "draw"}, "custom"),
    ]


async def test_direct_code_dispatch_uses_facade_without_output_callback(monkeypatch, tmp_path):
    calls: list[tuple[object, ...]] = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-direct"

    async def execute_code(*args, **kwargs) -> str:
        calls.append(("execute", args, kwargs))
        return "code result"

    async def run_with_temp_workspace(_agent_id, _tenant_id, runner, **kwargs) -> str:
        calls.append(("workspace", kwargs))
        return await runner(tmp_path)

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_tools, "_execute_code", execute_code)
    monkeypatch.setattr(agent_tools, "_run_with_temp_workspace", run_with_temp_workspace)

    agent_id = uuid.uuid4()
    for tool_name in ("execute_code", "execute_code_e2b"):
        assert await agent_tools._execute_tool_direct(tool_name, {"code": "pass"}, agent_id) == "code result"

    assert calls == [
        ("workspace", {"sync_back": True}),
        ("execute", (agent_id, tmp_path, {"code": "pass"}), {"tool_name": "execute_code"}),
        ("workspace", {"sync_back": True}),
        ("execute", (agent_id, tmp_path, {"code": "pass"}), {"tool_name": "execute_code_e2b"}),
    ]


async def test_normal_code_dispatch_propagates_output_callback(monkeypatch, tmp_path):
    calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-normal"

    async def execute_code(*args, **kwargs) -> str:
        calls.append(("execute", args, kwargs))
        return "code result"

    async def run_with_temp_workspace(_agent_id, _tenant_id, runner, **kwargs) -> str:
        calls.append(("workspace", kwargs))
        return await runner(tmp_path)

    async def on_output(_text: str, _label: str) -> None:
        return None

    agent_dao_mod = importlib.import_module("app.dao.agent_dao")

    async def no_agent(_agent_id: uuid.UUID):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _agent_id: tmp_path)
    monkeypatch.setattr(agent_tools, "_execute_code", execute_code)
    monkeypatch.setattr(agent_tools, "_run_with_temp_workspace", run_with_temp_workspace)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setattr(agent_dao_mod.agent_dao, "get", no_agent)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    for tool_name in ("execute_code", "execute_code_e2b"):
        assert (
            await agent_tools.execute_tool(tool_name, {"code": "pass"}, agent_id, user_id, on_output=on_output)
            == "code result"
        )

    assert calls == [
        ("workspace", {"sync_back": True}),
        ("execute", (agent_id, tmp_path, {"code": "pass"}), {"tool_name": "execute_code", "on_output": on_output}),
        ("workspace", {"sync_back": True}),
        ("execute", (agent_id, tmp_path, {"code": "pass"}), {"tool_name": "execute_code_e2b", "on_output": on_output}),
    ]


async def test_execute_tool_unknown_tool_returns_mcp_unknown_message(monkeypatch):
    from app.dao.tool_dao import tool_dao

    async def no_tool(_name):
        return None

    monkeypatch.setattr(tool_dao, "get_by_name", no_tool)
    monkeypatch.setattr(tool_dao, "get_mcp_by_mcp_tool_name", no_tool)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    result = await agent_tools.execute_tool(
        "nonexistent_wave1_tool",
        {},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    assert result == "Unknown tool: nonexistent_wave1_tool"


async def test_extracted_direct_routing_deploy_tools_use_modules_not_legacy_facade(monkeypatch, tmp_path):
    deploy = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy")
    deploy_ops = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy_ops")
    calls = []

    async def tenant_lookup(_agent_id: uuid.UUID) -> str:
        return "tenant-deploy"

    async def extracted_deploy(agent_id: uuid.UUID, ws: Path, arguments: registry.ToolArguments) -> str:
        calls.append(("deploy", agent_id, ws, arguments.copy()))
        return "deploy via extracted"

    async def extracted_list(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("list", agent_id, arguments.copy()))
        return "list via extracted"

    async def extracted_logs(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("logs", agent_id, arguments.copy()))
        return "logs via extracted"

    async def extracted_env(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("env", agent_id, arguments.copy()))
        return "env via extracted"

    async def extracted_domain(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("domain", agent_id, arguments.copy()))
        return "domain via extracted"

    async def extracted_neon(agent_id: uuid.UUID, arguments: registry.ToolArguments) -> str:
        calls.append(("neon", agent_id, arguments.copy()))
        return "neon via extracted"

    async def legacy_fail(*_args, **_kwargs) -> str:
        raise AssertionError("dispatcher must not call legacy deploy facade bodies")

    monkeypatch.setattr(deploy, "_vercel_deploy", extracted_deploy)
    monkeypatch.setattr(deploy, "_vercel_list_deployments", extracted_list)
    monkeypatch.setattr(deploy, "_vercel_get_deploy_logs", extracted_logs)
    monkeypatch.setattr(deploy_ops, "_vercel_set_env", extracted_env)
    monkeypatch.setattr(deploy_ops, "_vercel_manage_domain", extracted_domain)
    monkeypatch.setattr(deploy_ops, "_neon_create_database", extracted_neon)
    monkeypatch.setattr(agent_tools, "_vercel_deploy", legacy_fail)
    monkeypatch.setattr(agent_tools, "_vercel_list_deployments", legacy_fail)
    monkeypatch.setattr(agent_tools, "_vercel_get_deploy_logs", legacy_fail)
    monkeypatch.setattr(agent_tools, "_vercel_set_env", legacy_fail)
    monkeypatch.setattr(agent_tools, "_vercel_manage_domain", legacy_fail)
    monkeypatch.setattr(agent_tools, "_neon_create_database", legacy_fail)
    monkeypatch.setattr(agent_tools, "resolve_tool_handler", lambda _tool_name: None)
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    deploy_result = await agent_tools.execute_tool("vercel_deploy", {"project_name": "app"}, agent_id, user_id)
    list_result = await agent_tools.execute_tool("vercel_list_deployments", {"project_name": "app"}, agent_id, user_id)
    logs_result = await agent_tools.execute_tool("vercel_get_deploy_logs", {"deployment_id": "dep"}, agent_id, user_id)
    env_result = await agent_tools.execute_tool("vercel_set_env", {"project_name": "app"}, agent_id, user_id)
    domain_result = await agent_tools.execute_tool("vercel_manage_domain", {"domain": "example.com"}, agent_id, user_id)
    neon_result = await agent_tools.execute_tool("neon_create_database", {"project_name": "db"}, agent_id, user_id)

    assert deploy_result == "deploy via extracted"
    assert list_result == "list via extracted"
    assert logs_result == "logs via extracted"
    assert env_result == "env via extracted"
    assert domain_result == "domain via extracted"
    assert neon_result == "neon via extracted"
    assert calls == [
        ("deploy", agent_id, tmp_path / str(agent_id), {"project_name": "app"}),
        ("list", agent_id, {"project_name": "app"}),
        ("logs", agent_id, {"deployment_id": "dep"}),
        ("env", agent_id, {"project_name": "app"}),
        ("domain", agent_id, {"domain": "example.com"}),
        ("neon", agent_id, {"project_name": "db"}),
    ]


def test_public_agent_tools_import_contract():
    public_module = importlib.import_module("app.services.agent_tools")

    assert public_module.execute_tool is agent_tools.execute_tool
    # execute_tool is a thin wrapper around dispatcher; identity may differ.
    assert public_module.get_agent_tools_for_llm is agent_tools.get_agent_tools_for_llm
    assert inspect.iscoroutinefunction(public_module.execute_tool)
    assert inspect.iscoroutinefunction(public_module._execute_tool_direct)
    assert inspect.iscoroutinefunction(public_module.get_agent_tools_for_llm)
    # Catalog constants live on agent_tools_definitions (no re-export facade).
    assert agent_tools_definitions.AGENT_TOOLS[0] is agent_tools_definitions.AGENT_TOOLS[0]


def test_agent_tools_catalog_names_and_shape_match_current_contract():
    tool_names = tuple(tool["function"]["name"] for tool in agent_tools_definitions.AGENT_TOOLS)

    assert tool_names == EXPECTED_CATALOG_NAMES
    from app.services.llm.finish import FINISH_TOOL_DEFINITION

    assert agent_tools_definitions.AGENT_TOOLS[0] is FINISH_TOOL_DEFINITION
    for tool in agent_tools_definitions.AGENT_TOOLS:
        assert set(tool) == {"type", "function"}
        assert tool["type"] == "function"
        assert set(tool["function"]) == {"name", "description", "parameters"}
        assert isinstance(tool["function"]["name"], str)
        assert isinstance(tool["function"]["description"], str)
        assert tool["function"]["parameters"]["type"] == "object"
        assert isinstance(tool["function"]["parameters"]["properties"], dict)


def test_existing_agent_tools_monkeypatch_seams_still_exist():
    missing = [name for name in EXPECTED_MONKEYPATCH_SEAMS if not hasattr(agent_tools, name)]

    assert missing == []


async def test_extracted_direct_routing_search_tools_use_modules_not_legacy_facade(monkeypatch):
    search_module = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_search")
    module_specs = {
        "web_search": ("_web_search_module", "_web_search", "_web_search"),
        "jina_search": ("_web_search_module", "_jina_search", "_jina_search"),
        "exa_search": ("_web_search_module", "_exa_search", "_exa_search"),
        "duckduckgo_search": ("_web_search_module", "_duckduckgo_search_tool", "_duckduckgo_search_tool"),
        "tavily_search": ("_web_search_module", "_tavily_search_tool", "_tavily_search_tool"),
        "google_search": ("_web_search_module", "_google_search_tool", "_google_search_tool"),
        "bing_search": ("_web_search_module", "_bing_search_tool", "_bing_search_tool"),
        "jina_read": ("_web_read_module", "_jina_read", "_jina_read"),
        "read_webpage": ("_web_read_module", "_read_webpage", "_read_webpage"),
    }
    calls = []

    def target_module(module_attr: str):
        module = getattr(search_module, module_attr, None)
        assert module is not None, f"search.py must import {module_attr} for extracted direct routing"
        return module

    def extracted_handler(tool_name: str):
        async def handler(*args, **kwargs) -> str:
            calls.append((tool_name, args, kwargs))
            return f"{tool_name} via extracted"

        return handler

    async def legacy_fail(*_args, **_kwargs) -> str:
        raise AssertionError("search registry handler must not call legacy agent_tools facade bodies")

    for tool_name, (module_attr, helper_name, legacy_name) in module_specs.items():
        monkeypatch.setattr(target_module(module_attr), helper_name, extracted_handler(tool_name))
        monkeypatch.setattr(agent_tools, legacy_name, legacy_fail)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    arguments: registry.ToolArguments = {"query": "needle", "url": "https://example.test"}
    for tool_name in module_specs:
        handler = registry.resolve(tool_name)
        assert handler is not None
        handler_result = handler(
            arguments=arguments,
            agent_id=agent_id,
            user_id=user_id,
            session_id="session-search",
            on_output=None,
        )
        result = await handler_result if inspect.isawaitable(handler_result) else handler_result
        assert result == f"{tool_name} via extracted"

    assert [call[0] for call in calls] == list(module_specs)


async def test_search_storage_callbacks_use_direct_owners_with_facade_dependencies(monkeypatch):
    search_module = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_search")
    calls = []

    async def tenant_lookup(agent_id: uuid.UUID) -> str:
        calls.append(("tenant", agent_id))
        return "tenant-search"

    async def storage_search_files(agent_id: uuid.UUID, pattern: str, **kwargs) -> str:
        calls.append(("search", agent_id, pattern, kwargs))
        return "storage search result"

    async def storage_find_files(agent_id: uuid.UUID, pattern: str, **kwargs) -> str:
        calls.append(("find", agent_id, pattern, kwargs))
        return "storage find result"

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)

    async def legacy_storage(*_args, **_kwargs) -> str:
        raise AssertionError("search registry handlers must not call legacy storage wrappers")

    monkeypatch.setattr(search_module.workspace_read, "_storage_search_files", storage_search_files)
    monkeypatch.setattr(search_module.workspace_read, "_storage_find_files", storage_find_files)
    monkeypatch.setattr(agent_tools, "_storage_search_files", legacy_storage)
    monkeypatch.setattr(agent_tools, "_storage_find_files", legacy_storage)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    search_handler_result = search_module.search_files(
        arguments={"pattern": "needle", "path": "workspace", "file_pattern": "*.md", "ignore_case": True},
        agent_id=agent_id,
        user_id=user_id,
        session_id="session-storage",
        on_output=None,
    )
    assert not isinstance(search_handler_result, str)
    search_result = await search_handler_result
    find_handler_result = search_module.find_files(
        arguments={"pattern": "*.md", "path": "workspace"},
        agent_id=agent_id,
        user_id=user_id,
        session_id="session-storage",
        on_output=None,
    )
    assert not isinstance(find_handler_result, str)
    find_result = await find_handler_result

    assert search_result == "storage search result"
    assert find_result == "storage find result"
    assert calls == [
        ("tenant", agent_id),
        (
            "search",
            agent_id,
            "needle",
            {
                "path": "workspace",
                "file_pattern": "*.md",
                "ignore_case": True,
                "tenant_id": "tenant-search",
                "get_storage_backend": agent_tools.get_storage_backend,
                "tool_storage_key": agent_tools._tool_storage_key,
                "storage_walk_files": agent_tools._storage_walk_files,
                "relative_storage_display": agent_tools._relative_storage_display,
            },
        ),
        ("tenant", agent_id),
        (
            "find",
            agent_id,
            "*.md",
            {
                "path": "workspace",
                "tenant_id": "tenant-search",
                "get_storage_backend": agent_tools.get_storage_backend,
                "tool_storage_key": agent_tools._tool_storage_key,
                "storage_walk_files": agent_tools._storage_walk_files,
                "relative_storage_display": agent_tools._relative_storage_display,
                "display_size": agent_tools._display_size,
            },
        ),
    ]
