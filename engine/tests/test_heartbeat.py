import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.test_background_llm import BackgroundClient

from app.records.agent import AgentRecord
from app.services import heartbeat
from app.services.llm.types import LLMResponse, LLMToolCall, ToolPayload
from test_finish_protocol import _finish_response, _model


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

    async def _fresh_client(model, **_kwargs):
        return model, client

    monkeypatch.setattr("app.services.llm.create_fresh_llm_client_from_model", _fresh_client)
    monkeypatch.setattr(heartbeat.logger, "info", record_info)
    monkeypatch.setattr(heartbeat.logger, "exception", record_exception)

    # When: the one-shot has a zero-round budget.
    reply = await heartbeat.run_agent_oneshot(agent_id, "task", max_rounds=0)

    # Then: it completes normally and records zero executed rounds.
    assert reply == ""
    assert client.closed is True
    assert exception_messages == []
    assert any("completed (0 rounds" in message for message in info_messages)


@pytest.mark.parametrize("path", ["heartbeat", "oneshot"])
@pytest.mark.parametrize("budget", [0, 1, 2], ids=["already-capped", "midloop-cap", "finish"])
async def test_daily_quota_charges_each_heartbeat_request_and_skips_when_capped(monkeypatch, path, budget):
    # Given: both heartbeat entry points share a bounded agent daily budget.
    from app.dao.agent_dao import agent_dao
    from app.dao.notification_dao import notification_dao

    tenant_id = uuid.uuid4()
    model = replace(_model(), tenant_id=tenant_id)
    agent = AgentRecord(
        id=uuid.uuid4(), name="Heartbeat", creator_id=uuid.uuid4(), tenant_id=tenant_id, primary_model_id=model.id
    )
    finish = _finish_response("done")
    finish.usage = {"total_tokens": 17}
    client = BackgroundClient([LLMResponse(content="continue", usage={"total_tokens": 17}), finish])
    charge = AsyncMock(side_effect=[agent] * budget + [None])
    usage, activity, execute, notify = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(agent_dao, "consume_llm_call", charge, raising=False)
    monkeypatch.setattr(agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr("app.dao.llm_dao.llm_model_dao.get", AsyncMock(return_value=model))
    monkeypatch.setattr("app.dao.activity_log_dao.agent_activity_log_dao.list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(notification_dao, "list_unread_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(heartbeat, "get_storage_backend", lambda: SimpleNamespace(exists=AsyncMock(return_value=False)))
    monkeypatch.setattr("app.services.agent_context.build_agent_context", AsyncMock(return_value=("", "")))
    monkeypatch.setattr("app.services.agent_tools.get_agent_tools_for_llm", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.agent_tools.execute_tool", execute)
    monkeypatch.setattr("app.services.llm.create_fresh_llm_client_from_model", AsyncMock(return_value=(model, client)))
    monkeypatch.setattr("app.services.token_tracker.record_token_usage", usage)
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)
    monkeypatch.setattr(heartbeat, "_notify_oneshot_error", notify)
    complete = client.complete

    async def charged_complete(**kwargs):
        assert charge.await_count == len(client.messages_seen) + 1
        return await complete(**kwargs)

    monkeypatch.setattr(client, "complete", charged_complete)

    # When: the chosen entry point runs until finish or quota denial.
    if path == "heartbeat":
        await heartbeat._execute_heartbeat(agent.id)
    else:
        reply = await heartbeat.run_agent_oneshot(agent.id, "task")
        assert reply == ("done" if budget == 2 else "")

    # Then: refused requests never dispatch or post and prior token usage survives.
    assert len(client.messages_seen) == budget
    assert charge.await_count == (2 if budget == 2 else budget + 1)
    assert client.closed
    assert activity.await_count == int(budget == 2)
    assert usage.await_count == int(budget > 0)
    if budget:
        assert usage.await_args is not None
        assert usage.await_args.args[1].total_tokens == 17 * budget
    execute.assert_not_awaited()
    notify.assert_not_awaited()
