import base64
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import chat_sessions as chat_sessions_api
from app.core.domain.users import UserRole
from app.dao.chat_dao import SessionPosition, chat_session_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services.shared_chat import shared_session_id


@pytest.mark.parametrize("source", ["web", "agent", "shared"])
@pytest.mark.parametrize("viewer", ["owner", "member", "creator", "platform_admin", "org_admin", "agent_admin"])
async def test_history_access_matrix(monkeypatch, source, viewer):
    user = UserRecord(id=uuid.uuid4(), role=UserRole(viewer) if viewer not in ("owner", "creator") else UserRole.MEMBER)
    agent = AgentRecord(id=uuid.uuid4(), name="Agent", creator_id=user.id if viewer == "creator" else uuid.uuid4())
    session = ChatSessionRecord(
        id=shared_session_id(agent.id) if source == "shared" else uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id if viewer == "owner" else uuid.uuid4(),
        source_channel=source,
    )
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", AsyncMock(return_value=session))

    if viewer == "member" and source != "shared":
        with pytest.raises(HTTPException) as error:
            await chat_sessions_api._authorize_history(agent.id, session.id, user)
        assert error.value.status_code == 403
    else:
        assert await chat_sessions_api._authorize_history(agent.id, session.id, user) == session


@pytest.mark.parametrize("source", ["web", "agent", "shared"])
async def test_history_session_policy_cannot_bypass_agent_access(monkeypatch, source):
    user = UserRecord(id=uuid.uuid4(), role=UserRole.PLATFORM_ADMIN)
    agent_id = uuid.uuid4()
    session = ChatSessionRecord(
        id=shared_session_id(agent_id) if source == "shared" else uuid.uuid4(),
        agent_id=agent_id,
        user_id=user.id,
        source_channel=source,
    )
    lookup = AsyncMock(return_value=session)
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", AsyncMock(side_effect=HTTPException(403, "revoked")))
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", lookup)

    with pytest.raises(HTTPException) as error:
        await chat_sessions_api._authorize_history(agent_id, session.id, user)

    assert error.value.status_code == 403
    lookup.assert_not_awaited()


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
    shared = ChatSessionRecord(
        id=shared_session_id(agent_id),
        agent_id=agent_id,
        user_id=agent.creator_id,
        source_channel="shared",
        is_primary=True,
    )
    monkeypatch.setattr(chat_sessions_api, "ensure_shared_session", AsyncMock(return_value=shared))

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
    )

    assert len(sessions) == 2
    assert sessions[0].id == str(shared.id)
    assert sessions[1].id == str(session.id)
    assert sessions[1].user_id == str(owner_id)
    assert sessions[1].username == "Alice"


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
    shared = ChatSessionRecord(
        id=shared_session_id(agent_id), agent_id=agent_id, user_id=creator_id, source_channel="shared", is_primary=True
    )
    monkeypatch.setattr(chat_sessions_api, "ensure_shared_session", AsyncMock(return_value=shared))

    sessions = await chat_sessions_api.list_sessions(
        agent_id=agent_id,
        scope="all",
        current_user=current_user,
    )

    assert len(sessions) == 2
    assert sessions[0].id == str(shared.id)
    assert sessions[1].user_id == str(other_user_id)
    assert sessions[1].username == "Bob"


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
        attachments=(),
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
        before=cast("Any", None),
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
        attachments=(),
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
        before=cast("Any", None),
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
async def test_create_session_returns_shared_session_shape(monkeypatch):
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(UTC)
    current_user: Any = SimpleNamespace(id=user_id, role="member", tenant_id=uuid.uuid4())
    created = ChatSessionRecord(
        id=shared_session_id(agent_id),
        agent_id=agent_id,
        user_id=user_id,
        source_channel="shared",
        is_primary=True,
        title="Session 01-01 00:00",
        created_at=now,
    )

    async def fake_check_agent_access(_user, _agent_id):
        return SimpleNamespace(id=agent_id), "use"

    async def fake_create(_agent_id):
        return created

    monkeypatch.setattr(chat_sessions_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(chat_sessions_api, "ensure_shared_session", fake_create)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "message_counts", AsyncMock(return_value={}))

    result = await chat_sessions_api.create_session(
        agent_id=agent_id,
        body=chat_sessions_api.CreateSessionIn(),
        current_user=current_user,
    )

    assert result.agent_id == str(agent_id)
    assert result.user_id == str(user_id)
    assert result.source_channel == "shared"
    assert result.is_primary is True
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


# --- Cursor-paged session list -------------------------------------------------------------------------------------

EPOCH = datetime(2030, 1, 1, tzinfo=UTC)


def _order_key(session: ChatSessionRecord) -> tuple[bool, datetime, datetime, uuid.UUID]:
    """last_message_at DESC NULLS LAST, created_at DESC, id DESC as one ascending-is-newer key."""
    assert session.created_at is not None
    return (session.last_message_at is not None, session.last_message_at or EPOCH, session.created_at, session.id)


class _SessionStore:
    """In-memory model of chat_dao.list_page: scope rules, EXISTS filter, shared exclusion and keyset order."""

    def __init__(self, agent_id: uuid.UUID, viewer_id: uuid.UUID) -> None:
        self.agent_id = agent_id
        self.viewer_id = viewer_id
        self.shared = ChatSessionRecord(
            id=shared_session_id(agent_id),
            agent_id=agent_id,
            user_id=uuid.uuid4(),
            source_channel="shared",
            is_primary=True,
            created_at=EPOCH,
        )
        self.sessions: dict[uuid.UUID, ChatSessionRecord] = {self.shared.id: self.shared}
        self.counts: dict[str, int] = {str(self.shared.id): 0}
        self.page_calls: list[dict[str, Any]] = []

    def add(
        self,
        *,
        minutes: int | None,
        created: int,
        messages: int = 1,
        owner: uuid.UUID | None = None,
        channel: str = "web",
        peer_agent_id: uuid.UUID | None = None,
        is_group: bool = False,
        key: int | None = None,
    ) -> ChatSessionRecord:
        record = ChatSessionRecord(
            id=uuid.UUID(int=key) if key is not None else uuid.uuid4(),
            agent_id=self.agent_id,
            user_id=owner or self.viewer_id,
            title=f"Session {created}",
            source_channel=channel,
            peer_agent_id=peer_agent_id,
            is_group=is_group,
            created_at=EPOCH + timedelta(seconds=created),
            last_message_at=None if minutes is None else EPOCH + timedelta(minutes=minutes),
        )
        self.sessions[record.id] = record
        self.counts[str(record.id)] = messages
        return record

    def scoped(self, scope: str) -> list[ChatSessionRecord]:
        """Rows matching the scope rules, empty sessions included, newest activity first."""
        rows = [row for row in self.sessions.values() if row.id != self.shared.id]
        if scope == "mine":
            rows = [
                row
                for row in rows
                if row.user_id == self.viewer_id and not row.is_group and row.source_channel not in ("agent", "trigger")
            ]
        return sorted(rows, key=_order_key, reverse=True)

    def visible(self, scope: str) -> list[ChatSessionRecord]:
        return [row for row in self.scoped(scope) if self.counts[str(row.id)] > 0]

    async def list_page(
        self,
        *,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        scope: str,
        exclude_id: uuid.UUID,
        limit: int,
        after: SessionPosition | None = None,
    ) -> list[ChatSessionRecord]:
        self.page_calls.append({"scope": scope, "exclude_id": exclude_id, "limit": limit, "after": after})
        assert (agent_id, user_id, exclude_id) == (self.agent_id, self.viewer_id, self.shared.id)
        rows = self.visible(scope)
        if after is not None:
            boundary = (after.last_message_at is not None, after.last_message_at or EPOCH, after.created_at, after.id)
            rows = [row for row in rows if _order_key(row) < boundary]
        return rows[:limit]

    async def legacy_all(self, *_args: object, **_kwargs: object) -> list[ChatSessionRecord]:
        return self.scoped("all")

    async def legacy_mine(self, **_kwargs: object) -> list[ChatSessionRecord]:
        return self.scoped("mine")

    async def message_counts(self, ids: Sequence[str], *, agent_id: uuid.UUID | None = None) -> dict[str, int]:
        return {sid: count for sid in ids if (count := self.counts.get(sid, 0)) > 0}


def _viewer(role: str = "member", *, creator: bool = False) -> tuple[Any, Any]:
    viewer = SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=uuid.uuid4())
    agent = SimpleNamespace(id=uuid.uuid4(), creator_id=viewer.id if creator else uuid.uuid4())
    return viewer, agent


def _wire(monkeypatch: pytest.MonkeyPatch, store: _SessionStore, agent: Any) -> dict[str, AsyncMock]:
    class _Conn:
        async def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "identities" in sql:
                return [
                    {"id": row.user_id, "display": f"User {str(row.user_id)[:4]}"} for row in store.sessions.values()
                ]
            return []

    class _Ctx:
        async def __aenter__(self) -> _Conn:
            return _Conn()

        async def __aexit__(self, *_a: object) -> Literal[False]:
            return False

    ensure = AsyncMock(return_value=store.shared)
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", AsyncMock(return_value=(agent, "use")))
    monkeypatch.setattr(chat_sessions_api.agent_dao, "get", AsyncMock(return_value=agent))
    monkeypatch.setattr(chat_sessions_api, "ensure_shared_session", ensure)
    monkeypatch.setattr(chat_sessions_api, "connection_ctx", lambda: _Ctx())
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "list_page", store.list_page)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "list_all_for_agent", store.legacy_all)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "list_for_user", store.legacy_mine)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "message_counts", store.message_counts)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "unread_counts_for_user", AsyncMock(return_value={}))
    return {"ensure": ensure}


async def _page(
    store: _SessionStore,
    viewer: Any,
    *,
    scope: Literal["mine", "all"] = "all",
    limit: int = 2,
    cursor: str | None = None,
) -> chat_sessions_api.SessionPageOut:
    return await chat_sessions_api.list_sessions_page(
        agent_id=store.agent_id, scope=scope, limit=limit, cursor=cursor, current_user=viewer
    )


async def _walk(store: _SessionStore, viewer: Any, *, scope: Literal["mine", "all"], limit: int) -> list[list[str]]:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        page = await _page(store, viewer, scope=scope, limit=limit, cursor=cursor)
        pages.append([item.id for item in page.items])
        assert page.has_more == (page.next_cursor is not None)
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor


def _store(monkeypatch: pytest.MonkeyPatch, *, creator: bool = True) -> tuple[_SessionStore, Any]:
    viewer, agent = _viewer("member", creator=creator)
    store = _SessionStore(agent.id, viewer.id)
    _ = _wire(monkeypatch, store, agent)
    return store, viewer


@pytest.mark.parametrize("scope", ["mine", "all"])
@pytest.mark.parametrize("limit", [1, 2, 3, 100])
async def test_pages_have_stable_boundaries_and_concatenate_to_the_whole_ordered_list(monkeypatch, scope, limit):
    store, viewer = _store(monkeypatch)
    for index in range(7):
        _ = store.add(minutes=index, created=index)
    pages = await _walk(store, viewer, scope=scope, limit=limit)

    flat = [sid for page in pages for sid in page]
    expected = [str(row.id) for row in store.visible(scope)]
    assert flat == [str(store.shared.id), *expected]
    assert len(set(flat)) == len(flat)
    # The shared row rides on top of the first page; later pages hold at most `limit` rows, full until the last.
    assert len(pages[0]) == min(limit, len(expected)) + 1
    assert all(len(page) == limit for page in pages[1:-1])
    assert len(pages) == max(1, -(-len(expected) // limit))


async def test_a_page_ending_exactly_on_the_last_row_reports_no_more(monkeypatch):
    store, viewer = _store(monkeypatch)
    _ = [store.add(minutes=i, created=i) for i in range(4)]
    first = await _page(store, viewer, limit=4)
    assert (len(first.items), first.has_more, first.next_cursor) == (5, False, None)
    assert [call["limit"] for call in store.page_calls] == [5]  # limit + 1 probe row, never OFFSET/COUNT


async def test_null_last_message_at_sorts_last_and_ties_break_on_created_then_id(monkeypatch):
    store, viewer = _store(monkeypatch)
    newest = store.add(minutes=9, created=1)
    tied_old = store.add(minutes=5, created=1, key=6)
    tied_new = store.add(minutes=5, created=2, key=7)
    tied_id_low = store.add(minutes=5, created=3, key=2)
    tied_id_high = store.add(minutes=5, created=3, key=3)
    never_a = store.add(minutes=None, created=8, key=4)
    never_b = store.add(minutes=None, created=8, key=5)
    never_old = store.add(minutes=None, created=2)
    expected = [newest, tied_id_high, tied_id_low, tied_new, tied_old, never_b, never_a, never_old]

    for limit in (1, 2, 3):
        pages = await _walk(store, viewer, scope="all", limit=limit)
        assert [sid for page in pages for sid in page][1:] == [str(row.id) for row in expected]
    # A cursor sitting on a NULL row continues only through NULL rows.
    cursor = chat_sessions_api._session_cursor(store.agent_id, "all", never_b)
    third = await _page(store, viewer, limit=1, cursor=cursor)
    assert [item.id for item in third.items] == [str(never_a.id)]
    assert never_b.created_at is not None
    assert store.page_calls[-1]["after"] == SessionPosition(None, never_b.created_at, never_b.id)


async def test_empty_sessions_never_shorten_or_split_a_page(monkeypatch):
    store, viewer = _store(monkeypatch)
    kept = [store.add(minutes=i, created=i, messages=2) for i in range(6)]
    for i in range(6, 12):
        _ = store.add(minutes=i, created=i, messages=0)  # newer than every kept row, but empty
    pages = await _walk(store, viewer, scope="all", limit=3)

    assert [len(page) for page in pages] == [4, 3]
    assert [sid for page in pages for sid in page][1:] == [str(row.id) for row in reversed(kept)]
    # Filtering is the DAO's job: the handler never drops rows or re-counts them in Python.
    assert all(call["limit"] == 4 for call in store.page_calls)


async def test_shared_session_is_returned_once_on_the_first_page_only(monkeypatch):
    store, viewer = _store(monkeypatch)
    viewer_agent_calls = _wire(monkeypatch, store, SimpleNamespace(id=store.agent_id, creator_id=viewer.id))
    _ = [store.add(minutes=i, created=i) for i in range(5)]
    pages = await _walk(store, viewer, scope="all", limit=2)

    shared_id = str(store.shared.id)
    assert pages[0][0] == shared_id
    assert [page for page in pages if shared_id in page] == [pages[0]]
    assert all(call["exclude_id"] == store.shared.id for call in store.page_calls)
    viewer_agent_calls["ensure"].assert_awaited_once()
    first = await _page(store, viewer, limit=2)
    shared_row = first.items[0]
    assert (shared_row.source_channel, shared_row.is_primary, shared_row.unread_count) == ("shared", True, 0)


async def test_mine_scope_keeps_peer_and_group_rules_while_all_scope_lists_peer_agent_sessions(monkeypatch):
    store, viewer = _store(monkeypatch)
    other = uuid.uuid4()
    mine_web = store.add(minutes=1, created=1)
    others_web = store.add(minutes=2, created=2, owner=other)
    peer = store.add(minutes=3, created=3, owner=other, channel="agent", peer_agent_id=uuid.uuid4())
    group = store.add(minutes=4, created=4, is_group=True)
    trigger = store.add(minutes=5, created=5, channel="trigger")

    mine = await _walk(store, viewer, scope="mine", limit=10)
    everything = await _walk(store, viewer, scope="all", limit=10)

    assert [sid for page in mine for sid in page][1:] == [str(mine_web.id)]
    assert [sid for page in everything for sid in page][1:] == [
        str(row.id) for row in (trigger, group, peer, others_web, mine_web)
    ]
    peer_row = next(item for item in (await _page(store, viewer, limit=10)).items if item.id == str(peer.id))
    assert (peer_row.participant_type, peer_row.peer_agent_id) == ("agent", str(peer.peer_agent_id))


@pytest.mark.parametrize("scope", ["mine", "all"])
async def test_concatenated_pages_equal_todays_list_rows_and_fields(monkeypatch, scope):
    store, viewer = _store(monkeypatch)
    other = uuid.uuid4()
    _ = [store.add(minutes=i if i % 4 else None, created=i, owner=viewer.id if i % 2 else other) for i in range(11)]
    _ = store.add(minutes=3, created=40, owner=viewer.id, messages=0)  # empty: dropped by both paths
    legacy = await chat_sessions_api.list_sessions(agent_id=store.agent_id, scope=scope, current_user=viewer)

    paged: list[chat_sessions_api.SessionOut] = []
    cursor: str | None = None
    while True:
        page = await _page(store, viewer, scope=scope, limit=3, cursor=cursor)
        paged.extend(page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert [item.model_dump() for item in paged] == [item.model_dump() for item in legacy]


async def test_row_moving_up_mid_scroll_is_never_repeated(monkeypatch):
    store, viewer = _store(monkeypatch)
    rows = [store.add(minutes=i, created=i) for i in range(8)]  # newest first: rows[7], rows[6], ...
    first = await _page(store, viewer, limit=3)
    seen = [item.id for item in first.items][1:]
    assert seen == [str(rows[7].id), str(rows[6].id), str(rows[5].id)]

    # A row already shown and a row not yet shown both gain a message and jump to the top.
    store.sessions[rows[7].id].last_message_at = EPOCH + timedelta(days=1)
    store.sessions[rows[1].id].last_message_at = EPOCH + timedelta(days=2)
    rest: list[str] = []
    cursor = first.next_cursor
    while cursor is not None:
        page = await _page(store, viewer, limit=3, cursor=cursor)
        rest += [item.id for item in page.items]
        cursor = page.next_cursor

    assert not set(seen) & set(rest)
    assert len(set(rest)) == len(rest)
    # The unseen row that moved above the cursor may be missed until refresh, but nothing else is.
    assert rest == [str(rows[i].id) for i in (4, 3, 2, 0)]


@pytest.mark.parametrize("scope", ["mine", "all"])
async def test_scope_all_still_requires_session_viewing_rights(monkeypatch, scope):
    store, viewer = _store(monkeypatch, creator=False)
    if scope == "all":
        with pytest.raises(HTTPException) as denied:
            _ = await _page(store, viewer, scope="all")
        assert denied.value.status_code == 403
        assert store.page_calls == []
    else:
        assert (await _page(store, viewer, scope="mine")).items[0].id == str(store.shared.id)
    viewer.role = "agent_admin"
    assert (await _page(store, viewer, scope="all")).items[0].id == str(store.shared.id)


async def test_unknown_agent_is_404_and_agent_access_is_enforced_first(monkeypatch):
    store, viewer = _store(monkeypatch)
    monkeypatch.setattr(chat_sessions_api.agent_dao, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as missing:
        _ = await _page(store, viewer)
    assert missing.value.status_code == 404
    monkeypatch.setattr(chat_sessions_api.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(creator_id=None)))
    monkeypatch.setattr(chat_sessions_api, "check_agent_access", AsyncMock(side_effect=HTTPException(403, "no")))
    with pytest.raises(HTTPException) as forbidden:
        _ = await _page(store, viewer)
    assert forbidden.value.status_code == 403
    assert store.page_calls == []


def _encode(payload: object) -> str:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def test_cursors_round_trip_opaque_and_bound_to_agent_and_scope(monkeypatch):
    store, viewer = _store(monkeypatch)
    _ = [store.add(minutes=i, created=i) for i in range(3)]
    page = await _page(store, viewer, limit=1)
    assert page.next_cursor is not None
    assert "=" not in page.next_cursor
    assert page.next_cursor.isascii()
    assert str(store.agent_id) not in page.next_cursor  # opaque, not a readable query string
    decoded = json.loads(base64.urlsafe_b64decode(page.next_cursor + "=" * (-len(page.next_cursor) % 4)))
    assert (decoded["agent_id"], decoded["scope"]) == (str(store.agent_id), "all")


def _valid_cursor(issued_for: uuid.UUID, **overrides: object) -> dict[str, object]:
    return {
        "version": 1,
        "agent_id": str(issued_for),
        "scope": "all",
        "last_message_at": "2030-01-01T00:05:00Z",
        "created_at": "2030-01-01T00:00:01Z",
        "session_id": str(uuid.uuid4()),
    } | overrides


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda agent: "!!!not-base64!!!", id="not-base64"),
        pytest.param(lambda agent: _encode(b"\xff\xfe\x00"), id="not-utf8"),
        pytest.param(lambda agent: _encode(b"plain text"), id="not-json"),
        pytest.param(lambda agent: _encode([1, 2, 3]), id="wrong-shape"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent))[:-9], id="truncated"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, version=2)), id="wrong-version"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, agent_id=str(uuid.uuid4()))), id="other-agent"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, scope="mine")), id="other-scope"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, scope="everyone")), id="invented-scope"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, session_id="not-a-uuid")), id="tampered-id"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, created_at="2030-01-01T00:00:01")), id="naive-time"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent, created_at=None)), id="null-created"),
        pytest.param(lambda agent: _encode(_valid_cursor(agent) | {"extra": 1}), id="extra-field"),
    ],
)
async def test_malformed_tampered_or_foreign_cursors_are_client_errors_that_never_reach_the_database(
    monkeypatch, build: Callable[[uuid.UUID], str]
):
    store, viewer = _store(monkeypatch)
    with pytest.raises(HTTPException) as error:
        _ = await _page(store, viewer, cursor=build(store.agent_id))
    assert error.value.status_code == 400
    assert store.page_calls == []


async def test_http_surface_validates_parameters_and_never_500s_on_bad_cursors(monkeypatch):
    store, viewer = _store(monkeypatch)
    _ = [store.add(minutes=i, created=i) for i in range(3)]
    app = FastAPI()
    app.include_router(chat_sessions_api.router)
    app.dependency_overrides[chat_sessions_api.get_current_user] = lambda: viewer
    url = f"/api/agents/{store.agent_id}/sessions/page"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(url, params={"scope": "all", "limit": 2})
        bad = [
            await client.get(url, params={"limit": 0}),
            await client.get(url, params={"limit": 201}),
            await client.get(url, params={"scope": "everyone"}),
            await client.get(url, params={"cursor": "x" * 1025}),
            await client.get(url, params={"cursor": "garbage"}),
            await client.get(url, params={"cursor": ok.json()["next_cursor"][:-4]}),
        ]
        follow = await client.get(url, params={"scope": "all", "limit": 2, "cursor": ok.json()["next_cursor"]})
        wrong_scope = await client.get(url, params={"scope": "mine", "limit": 2, "cursor": ok.json()["next_cursor"]})

    assert ok.status_code == follow.status_code == 200
    assert [response.status_code for response in bad] == [422, 422, 422, 422, 400, 400]
    assert wrong_scope.status_code == 400
    assert set(ok.json()) == {"items", "next_cursor", "has_more"}
    assert ok.json()["items"][0]["id"] == str(store.shared.id)
    assert not {item["id"] for item in follow.json()["items"]} & {item["id"] for item in ok.json()["items"]}


def _sql_capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _Db:
        async def fetchall(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            calls.append((" ".join(sql.split()), params))
            return []

    class _Ctx:
        async def __aenter__(self) -> _Db:
            return _Db()

        async def __aexit__(self, *_a: object) -> Literal[False]:
            return False

    monkeypatch.setattr(chat_session_dao, "session", lambda: _Ctx())
    return calls


async def test_dao_query_filters_empties_in_sql_and_orders_by_the_full_key(monkeypatch):
    calls = _sql_capture(monkeypatch)
    agent_id, user_id, shared_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _ = await chat_session_dao.list_page(
        agent_id=agent_id, user_id=user_id, scope="all", exclude_id=shared_id, limit=51
    )
    _ = await chat_session_dao.list_page(
        agent_id=agent_id, user_id=user_id, scope="mine", exclude_id=shared_id, limit=51
    )
    (all_sql, all_params), (mine_sql, mine_params) = calls

    for sql in (all_sql, mine_sql):
        assert "EXISTS (SELECT 1 FROM chat_messages m WHERE m.conversation_id = cs.id::text" in sql
        assert "cs.id <> %(exclude_id)s" in sql
        assert sql.endswith(
            "ORDER BY cs.last_message_at DESC NULLS LAST, cs.created_at DESC, cs.id DESC LIMIT %(limit)s"
        )
        assert "OFFSET" not in sql
        assert "COUNT" not in sql
    assert "cs.peer_agent_id = %(agent_id)s AND cs.source_channel = 'agent'" in all_sql
    assert "user_id" not in all_params
    assert "m.agent_id" not in all_sql
    assert "cs.user_id = %(user_id)s" in mine_sql
    assert "cs.is_group IS FALSE" in mine_sql
    assert "cs.source_channel NOT IN ('agent', 'trigger')" in mine_sql
    assert "m.agent_id = %(agent_id)s" in mine_sql
    assert mine_params["user_id"] == user_id
    assert all_params["limit"] == 51


async def test_dao_cursor_predicate_has_an_explicit_null_branch(monkeypatch):
    calls = _sql_capture(monkeypatch)
    agent_id, user_id, shared_id, last_id = (uuid.uuid4() for _ in range(4))
    created = EPOCH + timedelta(seconds=3)
    last = EPOCH + timedelta(minutes=5)
    for position in (SessionPosition(last, created, last_id), SessionPosition(None, created, last_id)):
        _ = await chat_session_dao.list_page(
            agent_id=agent_id, user_id=user_id, scope="all", exclude_id=shared_id, limit=3, after=position
        )
    (non_null_sql, non_null_params), (null_sql, null_params) = calls

    assert (
        "AND (cs.last_message_at < %(c_last_message_at)s "
        "OR (cs.last_message_at = %(c_last_message_at)s AND (cs.created_at, cs.id) < (%(c_created_at)s, %(c_id)s)) "
        "OR cs.last_message_at IS NULL) ORDER BY" in non_null_sql
    )
    assert non_null_params["c_last_message_at"] == last
    assert non_null_params["c_id"] == last_id
    assert (
        "AND (cs.last_message_at IS NULL AND (cs.created_at, cs.id) < (%(c_created_at)s, %(c_id)s)) ORDER BY"
        in null_sql
    )
    assert "c_last_message_at" not in null_params
    assert null_params["c_created_at"] == created


async def test_session_lookup_serves_url_opened_sessions_with_list_row_shape(monkeypatch):
    store, viewer = _store(monkeypatch)
    archived = store.add(minutes=1, created=1)  # far below any loaded page
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", AsyncMock(return_value=archived))
    row = await chat_sessions_api.get_session(store.agent_id, archived.id, viewer)
    assert (row.id, row.agent_id, row.message_count, row.username) == (
        str(archived.id),
        str(store.agent_id),
        1,
        f"User {str(viewer.id)[:4]}",
    )
    assert row.model_dump().keys() == chat_sessions_api.SessionOut.model_fields.keys()


@pytest.mark.parametrize(
    ("role", "creator", "owner_is_viewer", "shared", "status"),
    [
        ("member", False, True, False, 200),
        ("member", False, False, True, 200),
        ("member", False, False, False, 403),
        ("org_admin", False, False, False, 200),
        ("member", True, False, False, 200),
    ],
)
async def test_session_lookup_applies_the_history_access_rules(
    monkeypatch, role, creator, owner_is_viewer, shared, status
):
    store, viewer = _store(monkeypatch, creator=creator)
    viewer.role = role
    record = (
        store.shared
        if shared
        else store.add(minutes=1, created=1, owner=viewer.id if owner_is_viewer else uuid.uuid4())
    )
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", AsyncMock(return_value=record))
    if status == 200:
        assert (await chat_sessions_api.get_session(store.agent_id, record.id, viewer)).id == str(record.id)
    else:
        with pytest.raises(HTTPException) as error:
            _ = await chat_sessions_api.get_session(store.agent_id, record.id, viewer)
        assert error.value.status_code == status


async def test_session_lookup_404s_for_unknown_or_other_agent_sessions(monkeypatch):
    store, viewer = _store(monkeypatch)
    monkeypatch.setattr(chat_sessions_api.chat_session_dao, "get_for_agent_or_peer", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as error:
        _ = await chat_sessions_api.get_session(store.agent_id, uuid.uuid4(), viewer)
    assert error.value.status_code == 404
