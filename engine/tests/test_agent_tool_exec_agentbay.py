from __future__ import annotations

import uuid
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agent_tool_exec import _agent_tool_exec_agentbay as agentbay
from app.services.agent_tool_exec.registry import ToolArguments, ToolExecutionContext, resolve, use_execution_context


def test_agentbay_helper_tuple_and_owner_map_are_complete() -> None:
    assert len(agentbay._AGENTBAY_HELPERS) == 33
    assert agentbay._AGENTBAY_HELPERS[0] == ("agentbay_browser_navigate", "_agentbay_browser_navigate")
    assert agentbay._AGENTBAY_HELPERS[-1] == ("agentbay_file_transfer", "_agentbay_file_transfer")
    helper_names = [name for _, name in agentbay._AGENTBAY_HELPERS]
    assert len(helper_names) == len(set(helper_names))
    assert set(helper_names) == set(agentbay._AGENTBAY_HELPER_MODULES)


@pytest.mark.asyncio
async def test_agentbay_registered_handler_uses_owner_module_context_and_original_arguments(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[uuid.UUID, Path, ToolArguments]] = []

    async def fake_helper(agent_id: uuid.UUID, workspace: Path, arguments: ToolArguments) -> str:
        calls.append((agent_id, workspace, arguments))
        arguments.pop("_session_id")
        return "owner result"

    module = SimpleNamespace(_agentbay_browser_navigate=fake_helper)
    monkeypatch.setattr(agentbay.importlib, "import_module", lambda name: module)
    arguments: ToolArguments = {"_session_id": "chat"}
    handler = resolve("agentbay_browser_navigate")
    assert handler is not None

    with use_execution_context(ToolExecutionContext(tenant_id="tenant", workspace_root=tmp_path)):
        pending = handler(
            arguments=arguments,
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            session_id="ignored",
            on_output=None,
        )
        assert isinstance(pending, Awaitable)
        result = await pending

    assert result == "owner result"
    assert calls[0][1] == tmp_path
    assert calls[0][2] is arguments
    assert arguments == {}


@pytest.mark.asyncio
async def test_agentbay_registered_handler_uses_workspace_fallback_and_propagates_exception(
    monkeypatch, tmp_path: Path
) -> None:
    agent_id = uuid.uuid4()
    expected_root = tmp_path / str(agent_id)

    async def failing_helper(*_args: object) -> str:
        raise LookupError("owner failure")

    module = SimpleNamespace(_agentbay_browser_navigate=failing_helper)
    monkeypatch.setattr(agentbay.importlib, "import_module", lambda name: module)
    monkeypatch.setattr(
        agentbay.workspace_paths,
        "_agent_workspace_root",
        lambda received_id, *, workspace_root: expected_root if received_id == agent_id else workspace_root,
    )
    handler = resolve("agentbay_browser_navigate")
    assert handler is not None

    pending = handler(arguments={}, agent_id=agent_id, user_id=uuid.uuid4(), session_id="", on_output=None)
    assert isinstance(pending, Awaitable)
    with pytest.raises(LookupError, match="owner failure"):
        await pending
