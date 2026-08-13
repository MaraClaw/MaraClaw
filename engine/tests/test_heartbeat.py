import uuid
from types import SimpleNamespace

import pytest

from app.services import heartbeat
from app.services.llm.types import LLMToolCall, ToolPayload


class _Client:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_tool_argument_preview_formats_mapping_arguments() -> None:
    # Given: a tool call with already-parsed mapping arguments.
    arguments: ToolPayload = {"recipient": "ops"}

    # When: heartbeat prepares the arguments for a bounded log preview.
    preview = heartbeat._tool_argument_preview(arguments, 20)

    # Then: the mapping is represented without slice-based failure.
    assert preview == "{'recipient': 'ops'}"


def test_tool_call_parts_rejects_missing_function() -> None:
    # Given: a malformed provider tool call with no function payload.
    tool_call: LLMToolCall = {"id": "call-1"}

    # When: heartbeat prepares the tool call for execution.
    # Then: it rejects the malformed call before execution.
    with pytest.raises(KeyError, match="function"):
        heartbeat._tool_call_parts(tool_call)


@pytest.mark.asyncio
async def test_run_agent_oneshot_logs_zero_completed_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an agent and client that reach the loop with no allowed rounds.
    agent_id = uuid.uuid4()
    model_id = uuid.uuid4()
    fake_agent = SimpleNamespace(
        primary_model_id=model_id,
        fallback_model_id=None,
        name="Heartbeat Agent",
        role_description=None,
        creator_id=None,
    )
    fake_model = SimpleNamespace(
        provider="openai",
        model="test-model",
        base_url=None,
        temperature=None,
        max_output_tokens=None,
        request_timeout=None,
    )
    client = _Client()
    info_messages: list[str] = []
    exception_messages: list[str] = []

    async def get_agent(_id: object) -> object:
        return fake_agent

    async def get_model(_id: object) -> object:
        return fake_model

    async def build_context(*args: object) -> tuple[str, str]:
        return "system", "dynamic"

    async def get_tools(*args: object) -> list[dict[str, object]]:
        return []

    def record_info(message: str) -> None:
        info_messages.append(message)

    def record_exception(message: str) -> None:
        exception_messages.append(message)

    from app.dao.agent_dao import agent_dao
    from app.dao.llm_dao import llm_model_dao

    monkeypatch.setattr(agent_dao, "get", get_agent)
    monkeypatch.setattr(llm_model_dao, "get", get_model)
    monkeypatch.setattr("app.services.agent_context.build_agent_context", build_context)
    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", get_tools)
    monkeypatch.setattr("app.services.llm.get_model_api_key", lambda model: "key")
    monkeypatch.setattr("app.services.llm.create_llm_client", lambda **kwargs: client)
    monkeypatch.setattr(heartbeat.logger, "info", record_info)
    monkeypatch.setattr(heartbeat.logger, "exception", record_exception)

    # When: the one-shot has a zero-round budget.
    reply = await heartbeat.run_agent_oneshot(agent_id, "task", max_rounds=0)

    # Then: it completes normally and records zero executed rounds.
    assert reply == ""
    assert client.closed is True
    assert exception_messages == []
    assert any("completed (0 rounds" in message for message in info_messages)
