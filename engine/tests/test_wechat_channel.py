import json
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from tests.im_webhook_fixtures import channel_config

from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import wechat_channel, wechat_message_processor
from app.services.channels import binding, submissions
from app.services.llm.types import OpenAIMessage
from app.services.openclaw_routing import PreparedOpenClawMessage


async def test_accept_wechat_event_converts_prior_tool_history_before_persisting_new_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = AgentRecord(id=uuid.uuid4(), name="agent", creator_id=uuid.uuid4(), tenant_id=uuid.uuid4())
    sender = UserRecord(id=uuid.uuid4(), tenant_id=agent.tenant_id)
    transcript = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=agent.creator_id)
    reply = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent.id, user_id=sender.id, source_channel="wechat")
    event = submissions.ChannelEvent(
        config_id=uuid.uuid4(),
        agent_id=agent.id,
        provider="wechat",
        event_key="wechat-bot:chat-1:message-1",
        sender_id="wechat-user",
        conversation_id="wechat_chat-1",
        text="Please check.",
    )
    tool_id = uuid.UUID(int=2)
    history_rows = [
        ChatMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(transcript.id),
            role="user",
            content="What is the status?",
        ),
        ChatMessageRecord(
            id=tool_id,
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(transcript.id),
            role="tool_call",
            content='{"name":"search_docs","args":{"query":"status"},"result":"found status"}',
        ),
    ]
    expected_history: list[OpenAIMessage] = [
        {"role": "user", "content": "What is the status?"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call_{tool_id}",
                    "type": "function",
                    "function": {"name": "search_docs", "arguments": json.dumps({"query": "status"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"call_{tool_id}", "content": "found status"},
    ]
    prepared = PreparedOpenClawMessage(agent, event.text, "primary", None, None, "channel-test")
    list_recent = AsyncMock(return_value=history_rows)
    prepare = AsyncMock(return_value=prepared)
    insert_message = AsyncMock(
        return_value=ChatMessageRecord(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=sender.id,
            conversation_id=str(transcript.id),
            role="user",
            content=event.text,
        )
    )
    enqueue = AsyncMock()
    order = Mock()
    order.attach_mock(list_recent, "history")
    order.attach_mock(prepare, "prepare")
    order.attach_mock(insert_message, "persist")
    order.attach_mock(enqueue, "enqueue")
    transaction = Mock(side_effect=AsyncMock)
    monkeypatch.setattr(submissions, "connection_ctx", transaction)
    monkeypatch.setattr(binding, "connection_ctx", transaction)
    monkeypatch.setattr(submissions, "_authorize", AsyncMock(return_value=(agent, sender)))
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "reserve", AsyncMock(return_value=True))
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "lock_conversation", AsyncMock())
    monkeypatch.setattr(submissions, "check_agent_expired", AsyncMock())
    monkeypatch.setattr(submissions, "charge_conversation_quota", AsyncMock())
    monkeypatch.setattr(submissions, "find_or_create_channel_session", AsyncMock(return_value=reply))
    monkeypatch.setattr(
        submissions,
        "bind_channel_conversation",
        AsyncMock(return_value=binding.ChannelConversation(transcript, reply)),
    )
    monkeypatch.setattr(submissions.chat_session_dao, "lock_for_agent", AsyncMock(return_value=transcript))
    monkeypatch.setattr(submissions.chat_message_dao, "list_recent", list_recent)
    monkeypatch.setattr(submissions.chat_message_dao, "insert_message", insert_message)
    monkeypatch.setattr(submissions.chat_session_dao, "get", AsyncMock(return_value=transcript))
    monkeypatch.setattr(submissions.chat_session_dao, "update", AsyncMock())
    monkeypatch.setattr(submissions, "prepare_openclaw_message", prepare)
    monkeypatch.setattr(submissions, "enqueue_prepared_openclaw_message", enqueue)

    result = await submissions.accept_channel_event(event)

    assert result == submissions.AcceptedChannelEvent(replayed=False)
    list_recent.assert_awaited_once_with(agent_id=agent.id, conversation_id=str(transcript.id), limit=8)
    prepare.assert_awaited_once_with(agent=agent, content="Please check.", history=expected_history)
    insert_message.assert_awaited_once_with(
        agent_id=agent.id,
        user_id=sender.id,
        conversation_id=str(transcript.id),
        role="user",
        content="Please check.",
        thinking=None,
        participant_id=None,
    )
    enqueue.assert_awaited_once_with(
        prepared,
        sender_user_id=sender.id,
        conversation_id=str(transcript.id),
        reply_session_id=reply.id,
        deduplicate=False,
    )
    assert [entry[0] for entry in order.mock_calls] == ["history", "prepare", "persist", "enqueue"]


async def test_process_wechat_message_remembers_context_before_accepting_native_identity_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = channel_config("wechat")
    config.extra_config = {"bot_token": "token", "baseurl": "https://wechat.example"}
    expected = submissions.ChannelEvent(
        config_id=config.id,
        agent_id=config.agent_id,
        provider="wechat",
        event_key="bot-app:chat-1:message-1",
        sender_id="wechat-user",
        conversation_id="wechat_chat-1",
        text="Please check.\nThen report.",
    )
    replay = AsyncMock(return_value=False)
    remember = AsyncMock()
    accept = AsyncMock(return_value=submissions.AcceptedChannelEvent(replayed=False))
    order = Mock()
    order.attach_mock(replay, "replay")
    order.attach_mock(remember, "remember")
    order.attach_mock(accept, "accept")
    monkeypatch.setattr(submissions, "is_channel_event_replay", replay)
    monkeypatch.setattr(wechat_channel, "remember_wechat_context", remember)
    monkeypatch.setattr(submissions, "accept_channel_event", accept)

    await wechat_message_processor.process_wechat_message(
        config.agent_id,
        {
            "message_id": "message-1",
            "from_user_id": "wechat-user",
            "session_id": "chat-1",
            "context_token": "context-token",
            "item_list": [
                {"type": 1, "text_item": {"text": "Please check."}},
                {"type": 1, "text_item": {"text": "Then report."}},
            ],
        },
        config,
    )

    replay.assert_awaited_once_with(expected)
    remember.assert_awaited_once_with(
        agent_id=config.agent_id,
        from_user_id="wechat-user",
        context_token="context-token",
        conv_id="wechat_chat-1",
    )
    accept.assert_awaited_once_with(expected)
    assert [entry[0] for entry in order.mock_calls] == ["replay", "remember", "accept"]
