import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.api import feishu
from app.services import activity_logger, wechat_channel, wechat_message_processor
from app.services.llm.types import OpenAIMessage


@dataclass(frozen=True, slots=True)
class _Agent:
    context_window_size: int


@dataclass(frozen=True, slots=True)
class _ChannelUser:
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _HistoryMessage:
    id: int
    role: str
    content: str
    thinking: str | None = None


class _ChannelSession:
    __slots__ = ("id", "last_message_at")

    def __init__(self, session_id: uuid.UUID) -> None:
        self.id = session_id
        self.last_message_at: datetime | None = None


async def test_process_wechat_message_converts_newest_first_tool_history_to_canonical_chronology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    events: list[str] = []
    captured_history: list[OpenAIMessage] = []
    inserted: list[str] = []
    channel_session = _ChannelSession(session_id)
    channel_user = _ChannelUser(user_id)
    history_rows = [
        _HistoryMessage(id=1, role="user", content="What is the status?"),
        _HistoryMessage(
            id=2,
            role="tool_call",
            content='{"name":"search_docs","args":{"query":"status"},"result":"found status"}',
        ),
    ]

    async def agent_get(_id: uuid.UUID) -> _Agent:
        events.append("agent-get")
        return _Agent(context_window_size=8)

    async def resolve_channel_user(
        *,
        db: object,
        agent: _Agent,
        channel_type: str,
        external_user_id: str,
        extra_info: dict[str, str],
    ) -> _ChannelUser:
        return channel_user

    async def find_channel_session(
        *,
        db: object,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        external_conv_id: str,
        source_channel: str,
        first_message_title: str,
    ) -> _ChannelSession:
        return channel_session

    async def remember_context(
        db: object,
        *,
        agent_id: uuid.UUID,
        from_user_id: str,
        context_token: str,
        conv_id: str,
    ) -> None:
        events.append("remember-context")

    async def list_recent(*, agent_id: uuid.UUID, conversation_id: str, limit: int):
        events.append("list-recent")
        return history_rows

    async def insert_message(**kwargs):
        inserted.append(kwargs["role"])
        events.append(f"insert:{kwargs['role']}")
        return

    async def session_update(*, db_obj, obj_in):
        events.append("session-update")
        return db_obj

    async def session_get(_id: uuid.UUID):
        return channel_session

    async def load_agent_and_model(
        db: object,
        requested_agent_id: uuid.UUID,
    ) -> tuple[_Agent, None, None]:
        events.append("load-model")
        return _Agent(context_window_size=8), None, None

    async def call_llm(
        agent: _Agent,
        model: None,
        fallback_model: None,
        requested_agent_id: uuid.UUID,
        user_text: str,
        *,
        history: list[OpenAIMessage] | None = None,
        user_id: uuid.UUID | None = None,
        session_id: str = "",
    ) -> str:
        assert history is not None
        captured_history.extend(history)
        events.append("llm")
        return "The status is available."

    async def send_message(
        *,
        token: str,
        base_url: str,
        to_user_id: str,
        context_token: str,
        text: str,
        route_tag: str | None = None,
    ) -> None:
        events.append("send")

    async def log_activity(
        logged_agent_id: uuid.UUID,
        action_type: str,
        summary: str,
        detail: dict[str, str] | None = None,
        related_id: uuid.UUID | None = None,
    ) -> None:
        events.append("activity")

    monkeypatch.setattr(wechat_message_processor.agent_dao, "get", agent_get)
    monkeypatch.setattr(wechat_message_processor.channel_user_service, "resolve_channel_user", resolve_channel_user)
    monkeypatch.setattr(wechat_message_processor, "find_or_create_channel_session", find_channel_session)
    monkeypatch.setattr(wechat_message_processor.chat_message_dao, "list_recent", list_recent)
    monkeypatch.setattr(wechat_message_processor.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(wechat_message_processor.chat_session_dao, "update", session_update)
    monkeypatch.setattr(wechat_message_processor.chat_session_dao, "get", session_get)
    monkeypatch.setattr(wechat_channel, "remember_wechat_context", remember_context)
    monkeypatch.setattr(wechat_channel, "send_wechat_text_message", send_message)
    monkeypatch.setattr(feishu, "_load_agent_and_model", load_agent_and_model)
    monkeypatch.setattr(feishu, "_call_llm_with_config", call_llm)
    monkeypatch.setattr(activity_logger, "log_activity", log_activity)
    config = SimpleNamespace(
        agent_id=agent_id,
        channel_type="wechat",
        extra_config={"bot_token": "token", "baseurl": "https://wechat.example"},
    )

    # When
    await wechat_message_processor.process_wechat_message(
        agent_id,
        {
            "from_user_id": "wechat-user",
            "session_id": "chat-1",
            "context_token": "context-token",
            "item_list": [{"type": 1, "text_item": {"text": "Please check."}}],
        },
        config,
    )

    # Then
    assert [message["role"] for message in captured_history] == ["user", "assistant", "tool"]
    tool_calls = captured_history[1].get("tool_calls")
    assert tool_calls is not None
    assert len(tool_calls) == 1
    tool_call = tool_calls[0]
    assert tool_call.get("id") == "call_2"
    assert tool_call.get("type") == "function"
    function = tool_call.get("function")
    assert function is not None
    assert function.get("name") == "search_docs"
    arguments = function.get("arguments")
    assert isinstance(arguments, str)
    assert json.loads(arguments) == {"query": "status"}
    assert captured_history[2].get("tool_call_id") == "call_2"
    assert captured_history[2].get("content") == "found status"
    assert inserted == ["user", "assistant"]
    assert events == [
        "agent-get",
        "remember-context",
        "list-recent",
        "insert:user",
        "session-update",
        "load-model",
        "llm",
        "send",
        "insert:assistant",
        "session-update",
        "activity",
    ]
