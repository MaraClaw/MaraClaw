from collections import Counter
from inspect import iscoroutinefunction

from app.services import tool_seeder

TABLE_COUNTS = {
    "BUILTIN_TOOLS": 124,
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
