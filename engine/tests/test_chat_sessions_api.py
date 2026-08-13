import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api import chat_sessions as chat_sessions_api


@pytest.mark.asyncio
async def test_org_admin_can_list_all_sessions(monkeypatch):
    viewer_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=viewer_id, role="org_admin", tenant_id=uuid.uuid4())
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

    async def fake_check_agent_access(_user, _agent_id, _db=None):
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

    current_user = SimpleNamespace(id=creator_id, role="member", tenant_id=uuid.uuid4())
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

    async def fake_check_agent_access(_user, _agent_id, _db=None):
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

    current_user = SimpleNamespace(id=viewer_id, role="org_admin", tenant_id=uuid.uuid4())
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        peer_agent_id=None,
        user_id=owner_id,
        source_channel="web",
        is_group=False,
    )
    message = SimpleNamespace(
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
        thinking=None,
    )

    async def fake_check_agent_access(_user, _agent_id, _db=None):
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
        before=None,
        current_user=current_user,
    )

    assert messages == [
        {
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
        }
    ]


@pytest.mark.asyncio
async def test_creator_can_view_other_users_session_messages(monkeypatch):
    creator_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    now = datetime.now(UTC)

    current_user = SimpleNamespace(id=creator_id, role="member", tenant_id=uuid.uuid4())
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
        role="user",
        content="hello",
        created_at=now,
        participant_id=None,
        thinking=None,
    )

    async def fake_check_agent_access(_user, _agent_id, _db=None):
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
        before=None,
        current_user=current_user,
    )

    assert messages == [
        {
            "role": "user",
            "content": "hello",
            "created_at": now.isoformat(),
        }
    ]


@pytest.mark.asyncio
async def test_create_session_returns_web_session_shape(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user = SimpleNamespace(id=user_id, role="member", tenant_id=uuid.uuid4())
    created = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        source_channel="web",
        title="Session 01-01 00:00",
        created_at=now,
    )

    async def fake_check_agent_access(_user, _agent_id, _db=None):
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
