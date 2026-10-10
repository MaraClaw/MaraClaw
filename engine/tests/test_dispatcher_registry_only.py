import inspect
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.services import activity_logger, agent_tools
from app.services.agent_tool_exec import mcp_tools, registry

FORMER_DUPLICATED_NAMES = [
    "manage_tasks",
    "send_platform_message",
    "send_channel_message",
    "send_channel_file",
    "plaza_get_new_posts",
    "plaza_create_post",
    "plaza_add_comment",
    "search_x",
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
    "publish_page",
    "list_published_pages",
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
]


@pytest.mark.parametrize("name", FORMER_DUPLICATED_NAMES)
def test_builtin_name_has_its_family_handler(name: str) -> None:
    # Given: normal facade imports populate the builtin registry.
    expected_family = "_agent_tool_exec_search" if name == "search_x" else "_agent_tool_exec_leftover"
    expected_handler = name
    if name.startswith("generate_image_"):
        expected_handler = "_generate"
    elif name in {"send_email", "read_emails", "reply_email"}:
        expected_handler = "_handle"
    # When: the builtin handler is resolved.
    handler = agent_tools.resolve_tool_handler(name)
    # Then: the correct owner and handler, including provider/email closures, is available.
    assert handler is not None
    assert handler.__module__ == f"app.services.agent_tool_exec.{expected_family}"
    assert inspect.isfunction(handler)
    assert handler.__name__ == expected_handler


@pytest.mark.parametrize("name", FORMER_DUPLICATED_NAMES)
async def test_missing_builtin_registration_does_not_execute_a_duplicate_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    # Given: a builtin name missing its registry entry and a dynamic MCP handler.
    agent_id = uuid4()
    calls: list[tuple[str, registry.ToolArguments, UUID | None]] = []

    async def tenant_lookup(_: UUID) -> str:
        return "tenant-registry-only"

    async def no_agent(_: UUID) -> None:
        return None

    async def dynamic(tool_name: str, arguments: registry.ToolArguments, agent_id: UUID | None = None) -> str:
        calls.append((tool_name, arguments, agent_id))
        return "dynamic only"

    async def log_activity(*_args: object, **_kwargs: object) -> None:
        return None

    from app.dao import agent_dao

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "_agent_workspace_root", lambda _: tmp_path)
    monkeypatch.delitem(registry.TOOL_HANDLERS, name)
    monkeypatch.setattr(agent_dao, "get", no_agent)
    monkeypatch.setattr(mcp_tools, "_execute_mcp_tool", dynamic)
    monkeypatch.setattr(activity_logger, "log_activity", log_activity)
    arguments: registry.ToolArguments = {"value": "input"}
    # When: the normal tool execution entrypoint is used.
    result = await agent_tools.execute_tool(name, arguments, agent_id, uuid4())
    # Then: there is no hidden builtin execution after failed resolution.
    assert result == "dynamic only"
    assert calls == [(name, arguments, agent_id)]
