from collections import Counter
from inspect import iscoroutinefunction

from app.services import tool_seeder

TABLE_COUNTS = {
    "BUILTIN_TOOLS": 126,
    "AGENTBAY_TOOLS": 33,
    "OKR_BUILTIN_TOOLS": 3,
    "DEPLOY_BUILTIN_TOOLS": 6,
}

REQUIRED_TOOL_KEYS = {
    "name",
    "display_name",
    "description",
    "category",
    "icon",
    "is_default",
    "parameters_schema",
    "config",
    "config_schema",
}

CURRENT_DUPLICATE_SEEDED_NAMES = {
    "get_okr": 2,
    "get_my_okr": 2,
    "update_kr_content": 2,
    "update_kr_progress": 2,
}

AGENT_TOOLS_ONLY_NAMES = {
    "feishu_wiki_list",
    "send_channel_message",
}

SEEDED_ONLY_SAMPLE_NAMES = {
    "agentbay_browser_extract",
    "agentbay_command_exec",
    "convert_html_to_pdf",
    "get_okr",
    "plaza_create_post",
    "vercel_deploy",
}


def _table_names(table):
    return [entry["name"] for entry in table]


def _seeded_names():
    return set(_table_names(tool_seeder.BUILTIN_TOOLS))


def _agent_tool_names():
    from app.services.agent_tools_definitions import AGENT_TOOLS

    return {entry["function"]["name"] for entry in AGENT_TOOLS}


def test_public_import_contract_exposes_tool_seeder_tables_and_functions():
    # Given: downstream startup code imports these names from tool_seeder.
    public_contract = {
        "BUILTIN_TOOLS": tool_seeder.BUILTIN_TOOLS,
        "AGENTBAY_TOOLS": tool_seeder.AGENTBAY_TOOLS,
        "OKR_BUILTIN_TOOLS": tool_seeder.OKR_BUILTIN_TOOLS,
        "DEPLOY_BUILTIN_TOOLS": tool_seeder.DEPLOY_BUILTIN_TOOLS,
    }
    public_functions = (
        tool_seeder.seed_builtin_tools,
        tool_seeder.clean_orphaned_mcp_tools,
        tool_seeder.seed_atlassian_rovo_config,
        tool_seeder.get_atlassian_api_key,
    )

    # When / Then: all table names import as lists and public functions remain async callables.
    assert set(public_contract) == set(TABLE_COUNTS)
    assert all(isinstance(table, list) for table in public_contract.values())
    assert all(iscoroutinefunction(function) for function in public_functions)


def test_tool_seeder_table_counts_match_current_source():
    # Given: the current seeder composes four public tool tables.
    observed_counts = {
        "BUILTIN_TOOLS": len(tool_seeder.BUILTIN_TOOLS),
        "AGENTBAY_TOOLS": len(tool_seeder.AGENTBAY_TOOLS),
        "OKR_BUILTIN_TOOLS": len(tool_seeder.OKR_BUILTIN_TOOLS),
        "DEPLOY_BUILTIN_TOOLS": len(tool_seeder.DEPLOY_BUILTIN_TOOLS),
    }

    # When / Then: the Wave 1 characterization pins the observed counts.
    assert observed_counts == TABLE_COUNTS


def test_each_public_tool_table_uses_the_current_required_key_shape():
    # Given: every current seed table entry uses the same top-level dictionary shape.
    public_tables = (
        tool_seeder.BUILTIN_TOOLS,
        tool_seeder.AGENTBAY_TOOLS,
        tool_seeder.OKR_BUILTIN_TOOLS,
        tool_seeder.DEPLOY_BUILTIN_TOOLS,
    )

    # When / Then: only the shape true of every current entry is asserted.
    for table in public_tables:
        for entry in table:
            assert set(entry) == REQUIRED_TOOL_KEYS
            assert isinstance(entry["name"], str)
            assert isinstance(entry["display_name"], str)
            assert isinstance(entry["description"], str)
            assert isinstance(entry["category"], str)
            assert isinstance(entry["icon"], str)
            assert isinstance(entry["is_default"], bool)
            assert isinstance(entry["parameters_schema"], dict)
            assert isinstance(entry["config"], dict)
            assert isinstance(entry["config_schema"], dict)


def test_seeded_tool_names_record_current_duplicate_names():
    # Given: current composed seeded names are not unique yet.
    duplicate_counts = {
        name: count for name, count in Counter(_table_names(tool_seeder.BUILTIN_TOOLS)).items() if count > 1
    }

    # When / Then: lock the existing duplicate surface for the later refactor wave.
    assert duplicate_counts == CURRENT_DUPLICATE_SEEDED_NAMES


def test_agent_tools_catalog_name_diff_is_documented_when_invariant_does_not_hold():
    # Given: app.services.agent_tools.AGENT_TOOLS is a separate OpenAI function catalog.
    seeded_names = _seeded_names()
    agent_tool_names = _agent_tool_names()

    # When: comparing the name sets shows the current invariant does not hold.
    agent_tools_only = agent_tool_names - seeded_names
    seeded_only = seeded_names - agent_tool_names

    # Then: record representative observed differences without enforcing a false equality invariant.
    assert agent_tools_only == AGENT_TOOLS_ONLY_NAMES
    assert seeded_only >= SEEDED_ONLY_SAMPLE_NAMES


def test_seed_builtin_tools_idempotency_is_infrastructure_heavy_in_current_shape():
    # Given: seed_builtin_tools owns creation, update, assignment, cleanup, and tenant migration SQL paths.
    seeded_names = _table_names(tool_seeder.BUILTIN_TOOLS)

    # When / Then: this wave locks the pure import/table surface instead of faking SQLAlchemy idempotency.
    assert iscoroutinefunction(tool_seeder.seed_builtin_tools)
    assert len(seeded_names) == TABLE_COUNTS["BUILTIN_TOOLS"]
    assert Counter(seeded_names)["finish"] == 1


def test_grok_is_the_default_image_provider():
    image_tools = [entry for entry in tool_seeder.BUILTIN_TOOLS if entry["name"].startswith("generate_image_")]
    default_image_tools = [entry["name"] for entry in image_tools if entry["is_default"]]

    assert default_image_tools == ["generate_image_grok"]
    assert "Prefer this over other generate_image_*" in next(
        entry["description"] for entry in tool_seeder.BUILTIN_TOOLS if entry["name"] == "generate_image_grok"
    )
    assert "generate_image_grok" in tool_seeder.SYNC_IS_DEFAULT_TOOL_NAMES


def test_search_x_is_a_default_search_tool():
    search_x = next(entry for entry in tool_seeder.BUILTIN_TOOLS if entry["name"] == "search_x")

    assert search_x["is_default"] is True
    assert search_x["category"] == "search"
    assert "search_x" in tool_seeder.SYNC_IS_DEFAULT_TOOL_NAMES


BUILTIN_TOOL_NAME_ORDER = [
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
    "read_document",
    "convert_csv_to_xlsx",
    "convert_html_to_pdf",
    "convert_html_to_pptx",
    "convert_markdown_to_docx",
    "convert_markdown_to_pdf",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "list_triggers",
    "send_channel_file",
    "send_platform_message",
    "send_message_to_agent",
    "send_file_to_agent",
    "read_webpage",
    "search_x",
    "plaza_get_new_posts",
    "plaza_create_post",
    "plaza_add_comment",
    "execute_code",
    "execute_code_e2b",
    "upload_image",
    "generate_image_siliconflow",
    "generate_image_openai",
    "generate_image_google",
    "generate_image_grok",
    "generate_image_custom",
    "discover_resources",
    "import_mcp_server",
    "send_email",
    "read_emails",
    "reply_email",
    "get_okr",
    "get_my_okr",
    "update_kr_progress",
    "update_kr_content",
    "collect_okr_progress",
    "generate_okr_report",
    "get_okr_settings",
    "create_objective",
    "create_key_result",
    "update_objective",
    "update_any_kr_progress",
    "generate_monthly_okr_report",
    "upsert_member_daily_report",
    "send_feishu_message",
    "feishu_user_search",
    "bitable_create_app",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
    "publish_page",
    "list_published_pages",
    "search_clawhub",
    "install_skill",
    "update_kr_content",
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
    "agentbay_computer_click",
    "agentbay_computer_precision_screenshot",
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
    "agentbay_computer_activate_window",
    "agentbay_computer_list_windows",
    "agentbay_computer_close_window",
    "agentbay_computer_dismiss_dialog",
    "agentbay_computer_list_visible_apps",
    "agentbay_file_transfer",
    "get_okr",
    "get_my_okr",
    "update_kr_progress",
    "vercel_deploy",
    "vercel_list_deployments",
    "vercel_get_deploy_logs",
    "vercel_set_env",
    "vercel_manage_domain",
    "neon_create_database",
]

AGENT_TOOLS_NAME_ORDER = [
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
    "read_webpage",
    "search_x",
    "read_document",
    "execute_code",
    "execute_code_e2b",
    "upload_image",
    "generate_image_siliconflow",
    "generate_image_openai",
    "generate_image_google",
    "generate_image_grok",
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
]


def test_builtin_tools_preserve_current_name_order():
    assert _table_names(tool_seeder.BUILTIN_TOOLS) == BUILTIN_TOOL_NAME_ORDER


def test_agent_tools_catalog_preserves_current_name_order():
    from app.services.agent_tools_definitions import AGENT_TOOLS

    assert [entry["function"]["name"] for entry in AGENT_TOOLS] == AGENT_TOOLS_NAME_ORDER
