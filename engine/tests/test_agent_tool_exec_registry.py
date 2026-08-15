from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.agent_tool_exec import registry
from app.services.agent_tool_exec.registry import ToolArguments, ToolHandler, ToolOutputCallback


@pytest.fixture(autouse=True)
def restore_tool_handlers() -> Iterator[dict[str, ToolHandler]]:
    importlib.import_module("app.services.agent_tools")
    saved_handlers = registry.TOOL_HANDLERS.copy()
    registry.TOOL_HANDLERS.clear()
    yield saved_handlers
    registry.TOOL_HANDLERS.clear()
    registry.TOOL_HANDLERS.update(saved_handlers)


async def _tenant_none(_agent_id: uuid.UUID) -> None:
    return None


async def _noop_log_activity(*_args, **_kwargs) -> None:
    return None


def test_register_returns_handler_and_resolve_finds_it() -> None:
    # Given: an empty execution registry.
    def sync_handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        return f"registered:{arguments['value']}:{agent_id}:{user_id}:{session_id}:{on_output is None}"

    # When: a handler is registered by decorator.
    registered_handler = registry.register("example_tool")(sync_handler)

    # Then: the decorator preserves the handler and resolve returns it by name.
    assert registered_handler is sync_handler
    assert registry.resolve("example_tool") is sync_handler


def test_resolve_returns_none_for_missing_name() -> None:
    # Given: an empty execution registry.

    # When: callers resolve an unregistered name.
    handler = registry.resolve("missing_tool")

    # Then: missing handlers are represented by None for legacy fallback.
    assert handler is None


def test_representative_handlers_remain_registered_after_facade_import(
    restore_tool_handlers: dict[str, ToolHandler],
) -> None:
    registry.TOOL_HANDLERS.update(restore_tool_handlers)

    for tool_name in frozenset(
        {
            "send_message_to_agent",
            "agentbay_browser_navigate",
            "convert_csv_to_xlsx",
            "send_feishu_message",
            "read_webpage",
            "search_x",
            "list_files",
            "set_trigger",
        }
    ):
        assert registry.resolve(tool_name) is not None


def test_duplicate_registration_raises_clear_error() -> None:
    # Given: a registered handler name.
    async def first_handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        return "first"

    async def second_handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        return "second"

    registry.register("duplicated_tool")(first_handler)

    # When / Then: registering the same name again raises a clear duplicate error.
    with pytest.raises(registry.DuplicateToolHandlerError, match="duplicated_tool"):
        registry.register("duplicated_tool")(second_handler)


async def test_execute_tool_uses_registered_sync_handler(monkeypatch, tmp_path) -> None:
    # Given: a sync handler registered for a tool name.
    agent_id = uuid.uuid4()
    tenant_id = "tenant-sync"
    contexts_seen = []

    async def tenant_lookup(observed_agent_id: uuid.UUID) -> str:
        assert observed_agent_id == agent_id
        return tenant_id

    def sync_handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        context = registry.current_execution_context()
        contexts_seen.append(context)
        assert context is not None
        return (
            f"sync:{arguments['value']}:{context.tenant_id}:{context.workspace_root}:{session_id}:{on_output is None}"
        )

    registry.register("registered_sync_tool")(sync_handler)

    from app.services import activity_logger, agent_tools

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_lookup)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    # When: execute_tool receives the registered name.
    result = await agent_tools.execute_tool(
        "registered_sync_tool",
        {"value": "ok"},
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id="session-1",
    )

    # Then: the registry handler receives the dispatcher context and its result is returned.
    expected_workspace = tmp_path / str(agent_id)
    assert result == f"sync:ok:{tenant_id}:{expected_workspace}:session-1:True"
    assert contexts_seen[0] == registry.ToolExecutionContext(tenant_id=tenant_id, workspace_root=expected_workspace)
    assert registry.current_execution_context() is None


async def test_execute_tool_uses_registered_async_handler(monkeypatch) -> None:
    # Given: an async handler registered for a tool name.
    async def async_handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        return f"async:{arguments['value']}:{agent_id != user_id}:{session_id}:{on_output is None}"

    registry.register("registered_async_tool")(async_handler)

    from app.services import activity_logger, agent_tools

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _tenant_none)
    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()

    # When: execute_tool receives the registered name.
    result = await agent_tools.execute_tool(
        "registered_async_tool",
        {"value": "ok"},
        agent_id=agent_id,
        user_id=user_id,
        session_id="session-2",
    )

    # Then: the awaited registry handler result is returned instead of the legacy chain.
    assert result == "async:ok:True:session-2:True"


async def test_execute_tool_checks_autonomy_before_registered_handler(monkeypatch) -> None:
    # Given: a registered handler under an autonomy-gated tool name.
    handler_called = False

    async def write_handler(
        *,
        arguments: ToolArguments,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str,
        on_output: ToolOutputCallback | None,
    ) -> str:
        nonlocal handler_called
        handler_called = True
        return "handler ran"

    registry.register("write_file")(write_handler)

    from app.dao.agent_dao import agent_dao
    from app.services import agent_tools, autonomy_service as autonomy_module

    async def fake_get_agent(_agent_id):
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _tenant_none)
    monkeypatch.setattr(agent_dao, "get", fake_get_agent)
    check_and_enforce = AsyncMock(return_value={"allowed": False, "level": "L3", "approval_id": "approval-123"})
    monkeypatch.setattr(autonomy_module.autonomy_service, "check_and_enforce", check_and_enforce)

    # When: execute_tool receives the registered, gated name.
    result = await agent_tools.execute_tool(
        "write_file",
        {"path": "workspace/a.txt", "content": "hello"},
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )

    # Then: autonomy denial returns before the registered handler can mutate anything.
    assert result == (
        "⏳ This action requires approval. An approval request has been sent. "
        "Please wait for approval before retrying. (Approval ID: approval-123)"
    )
    assert handler_called is False
    check_and_enforce.assert_awaited_once()
