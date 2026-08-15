import ast
import uuid

import pytest

from app.services.agent_tools import ToolParameters
from app.services.autonomy_service import AutonomyService


@pytest.mark.asyncio
async def test_execute_approved_action_rejects_serialized_non_object_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_executor(
        _tool_name: str,
        _arguments: ToolParameters,
        _agent_id: uuid.UUID,
    ) -> str:
        raise AssertionError("The direct executor must not receive non-object approval arguments")

    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", direct_executor)

    result = await AutonomyService()._execute_approved_action(
        uuid.uuid4(),
        "read_webpage",
        {"tool": "read_webpage", "args": "[]"},
    )

    assert result == "Execution failed: approved action arguments must be a JSON object"


@pytest.mark.asyncio
async def test_execute_approved_action_rejects_malformed_serialized_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_executor(
        _tool_name: str,
        _arguments: ToolParameters,
        _agent_id: uuid.UUID,
    ) -> str:
        raise AssertionError("The direct executor must not receive malformed approval arguments")

    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", direct_executor)

    result = await AutonomyService()._execute_approved_action(
        uuid.uuid4(),
        "read_webpage",
        {"tool": "read_webpage", "args": "{"},
    )

    assert result == "Execution failed: approved action arguments must be a JSON object"


@pytest.mark.asyncio
async def test_execute_approved_action_dispatches_serialized_mapping_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    expected_arguments: ToolParameters = {"query": "release notes"}
    received_arguments: list[ToolParameters] = []

    def parse_arguments(serialized_arguments: str) -> ToolParameters:
        assert serialized_arguments == "{'query': 'release notes'}"
        return expected_arguments

    async def direct_executor(
        observed_tool_name: str,
        observed_arguments: ToolParameters,
        observed_agent_id: uuid.UUID,
    ) -> str:
        assert observed_tool_name == "read_webpage"
        assert observed_agent_id == agent_id
        received_arguments.append(observed_arguments)
        return "executor sentinel"

    monkeypatch.setattr(ast, "literal_eval", parse_arguments)
    monkeypatch.setattr("app.services.agent_tool_exec.dispatcher._execute_tool_direct", direct_executor)

    result = await AutonomyService()._execute_approved_action(
        agent_id,
        "read_webpage",
        {"tool": "read_webpage", "args": "{'query': 'release notes'}"},
    )

    assert result == "executor sentinel"
    assert received_arguments == [expected_arguments]
    assert received_arguments[0] is expected_arguments
