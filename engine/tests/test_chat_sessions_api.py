import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import chat_sessions as chat_sessions_api
from app.records.chat import ChatMessageRecord, ChatSessionRecord


@pytest.mark.asyncio
async def test_org_admin_can_list_all_sessions(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user: Any = SimpleNamespace(id=viewer_id, role="org_admin", tenant_id=uuid.uuid4())
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=owner_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
        is_primary=False,
    )

    async def fake_check_agent_access(_user, _agent_id):
        return agent, "use"

    async def fake_agent_get(_id):
        return agent

    async def fake_list_all(_agent_id):
        return [session]

    async def fake_message_counts(_ids, agent_id=None):
        return {str(session.id): 3}

    async def fake_unread_counts(*, session_ids, user_id, mine_only=False):
        return {}

    class _Conn:
        async def fetchall(self, sql, params=None):
            if "identities" in sql:
                return [{"id": owner_id, "display": "Alice"}]
            return []

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api.agent_dao, "get", fake_agent_get)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "list_all_for_agent", fake_list_all)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "message_counts", fake_message_counts)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "unread_counts_for_user", fake_unread_counts)
    monkeypatch.setattr(chat_sessions_api, "connection_ctx", lambda: _Ctx())

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
    )

    assert len(sessions) == 1
    assert sessions[0].id == str(session.id)
    assert sessions[0].user_id == str(owner_id)
    assert sessions[0].username == "Alice"


@pytest.mark.asyncio
async def test_creator_can_list_all_sessions(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user: Any = SimpleNamespace(id=creator_id, role="member", tenant_id=uuid.uuid4())
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=other_user_id,
        source_channel="web",
        title="Customer follow-up",
        created_at=now,
        last_message_at=now,
        peer_agent_id=None,
        is_group=False,
        group_name=None,
        is_primary=False,
    )

    async def fake_check_agent_access(_user, _agent_id):
        return agent, "manage"

    async def fake_agent_get(_id):
        return agent

    async def fake_list_all(_agent_id):
        return [session]

    async def fake_message_counts(_ids, agent_id=None):
        return {str(session.id): 2}

    async def fake_unread_counts(*, session_ids, user_id, mine_only=False):
        return {}

    class _Conn:
        async def fetchall(self, sql, params=None):
            if "identities" in sql:
                return [{"id": other_user_id, "display": "Bob"}]
            return []

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api.agent_dao, "get", fake_agent_get)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "list_all_for_agent", fake_list_all)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "message_counts", fake_message_counts)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "unread_counts_for_user", fake_unread_counts)
    monkeypatch.setattr(chat_sessions_api, "connection_ctx", lambda: _Ctx())

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
    )

    assert len(sessions) == 1
    assert sessions[0].user_id == str(other_user_id)
    assert sessions[0].username == "Bob"


@pytest.mark.asyncio
async def test_org_admin_can_view_other_users_session_messages(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user: Any = SimpleNamespace(id=viewer_id, role="org_admin", tenant_id=uuid.uuid4())
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
        is_group=False,
    )
    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
        thinking=None,
        client_submission_id=None,
    )

    async def fake_check_agent_access(_user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=uuid.uuid4()), "use"

    async def fake_get_session(_sid, _aid):
        return session

    async def fake_list_messages(*, conversation_id, limit, before=None):
        return [message]

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", fake_get_session)
    monkeypatch.setattr(chat_sessions_api.chat_message_dao, "list_for_session", fake_list_messages)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        limit=20,
        before=cast(Any, None),
        current_user=current_user,
    )

    assert messages == [
        {
            "message_id": str(message.id),
            "entry_id": str(message.id),
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
            "client_submission_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_creator_can_view_other_users_session_messages(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user: Any = SimpleNamespace(id=creator_id, role="member", tenant_id=uuid.uuid4())
    agent = SimpleNamespace(id=agent_id, creator_id=creator_id)
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=other_user_id,
        source_channel="web",
        is_group=False,
    )
    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
        thinking=None,
        client_submission_id=None,
    )

    async def fake_check_agent_access(_user, _agent_id):
        return agent, "manage"

    async def fake_get_session(_sid, _aid):
        return session

    async def fake_list_messages(*, conversation_id, limit, before=None):
        return [message]

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", fake_get_session)
    monkeypatch.setattr(chat_sessions_api.chat_message_dao, "list_for_session", fake_list_messages)

    messages = await chat_sessions_api.get_session_messages(
        agent_id=agent_id,
        session_id=session_id,
        limit=20,
        before=cast(Any, None),
        current_user=current_user,
    )

    assert messages == [
        {
            "message_id": str(message.id),
            "entry_id": str(message.id),
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
            "client_submission_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_create_session_returns_web_session_shape(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user: Any = SimpleNamespace(id=user_id, role="member", tenant_id=uuid.uuid4())
    created = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        title="Session 01-01 00:00",
        created_at=now,
    )

    async def fake_check_agent_access(_user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    async def fake_create(*, obj_in):
        return created

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "create", fake_create)

    result = await chat_sessions_api.create_session(
        agent_id=agent_id,
        body=chat_sessions_api.CreateSessionIn(),
        current_user=current_user,
    )

    assert result.agent_id == str(agent_id)
    assert result.user_id == str(user_id)
    assert result.source_channel == "web"
    assert result.is_primary is False
    assert result.message_count == 0


@pytest.mark.parametrize("source_channel", ["web", "agent"])
async def test_history_http_preserves_content_and_deterministic_persisted_identities(monkeypatch, source_channel):
    agent_id, owner_id = uuid.uuid4(), uuid.uuid4()
    viewer = SimpleNamespace(id=uuid.uuid4(), role="org_admin")
    session = ChatSessionRecord(id=uuid.uuid4(), agent_id=agent_id, user_id=owner_id, source_channel=source_channel)
    now = datetime(2026, 10, 16, tzinfo=UTC)
    inline = 'before\n```tool_code\nsearch\n```\n```json\n{"q":"hello"}\n```\nafter'
    messages = [
        ChatMessageRecord(
            id=uuid.uuid4(), agent_id=agent_id, user_id=owner_id, role=role, content=content, created_at=now
        )
        for role, content in [
            ("user", "same"),
            ("assistant", "same"),
            ("tool_call", '{"name":"search","args":{"q":"hello"},"result":"found"}'),
            ("tool_call", "invalid JSON"),
            ("assistant", inline),
            ("assistant", "```tool_code malformed"),
        ]
    ]
    messages[1].thinking = "reasoning"
    messages[0].client_submission_id = uuid.uuid4()

    async def access(_user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=uuid.uuid4()), "use"

    async def get_session(_sid, _aid):
        return session

    async def list_messages(**kwargs):
        assert kwargs == {"conversation_id": str(session.id), "limit": 20, "before": None}
        return messages

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", access)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", get_session)
    monkeypatch.setattr(chat_sessions_api.chat_message_dao, "list_for_session", list_messages)
    app = FastAPI()
    app.include_router(chat_sessions_api.router)
    app.dependency_overrides[chat_sessions_api.get_current_user] = lambda: viewer
    url = f"/api/agents/{agent_id}/sessions/{session.id}/messages"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(url)
        second = await client.get(url)
        viewer.role = "member"
        forbidden = await client.get(url)

    assert first.status_code == second.status_code == 200
    entries = first.json()
    assert second.json() == entries
    assert forbidden.status_code == 403
    assert [entry["message_id"] for entry in entries[:4]] == [str(message.id) for message in messages[:4]]
    assert [entry["entry_id"] for entry in entries[:4]] == [str(message.id) for message in messages[:4]]
    assert entries[0]["content"] == entries[1]["content"] == "same"
    assert entries[0]["client_submission_id"] == str(messages[0].client_submission_id)
    assert "client_submission_id" not in entries[1]
    assert entries[1]["thinking"] == "reasoning"
    assert entries[2]["toolName"] == "search"
    assert entries[2]["toolArgs"] == {"q": "hello"}
    assert entries[2]["toolResult"] == "found"
    assert entries[3]["content"] == "invalid JSON"
    if source_channel == "agent":
        assert [entry["message_id"] for entry in entries[4:7]] == [str(messages[4].id)] * 3
        assert [entry["entry_id"] for entry in entries[4:7]] == [f"{messages[4].id}:{index}" for index in range(3)]
        assert [entry["content"] for entry in entries[4:7]] == ["before", "", "after"]
        assert entries[5]["toolName"] == "search"
        assert entries[5]["toolArgs"] == {"q": "hello"}
        assert entries[7]["entry_id"] == f"{messages[5].id}:0"
    else:
        assert entries[4]["content"] == inline
        assert entries[4]["entry_id"] == str(messages[4].id)
        assert entries[5]["entry_id"] == str(messages[5].id)
    assert entries[-1]["content"] == "```tool_code malformed"
    assert len({entry["entry_id"] for entry in entries}) == len(entries)
