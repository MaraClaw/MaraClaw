from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from tests.test_redis_cache import FakeRedis

from app.api import chat_sessions
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord, ChatSessionRecord
from app.records.user import UserRecord
from app.services import chat_history
from app.services.chat_message_projection import project_messages

pytest_plugins = ["tests.test_redis_cache"]


class HistoryHarness:
    def __init__(self, cache: FakeRedis):
        self.cache = cache
        self.viewer = UserRecord(id=uuid4(), role=UserRole.MEMBER)
        self.agent = AgentRecord(id=uuid4(), name="History", creator_id=uuid4())
        self.session = ChatSessionRecord(id=uuid4(), agent_id=self.agent.id, user_id=self.viewer.id)
        self.timestamp = datetime(2026, 10, 24, 12, 0, 0, 123456, tzinfo=UTC)
        self.rows = [self.message(index) for index in range(1, 6)]
        self.revision: str | None = str(uuid4())
        self.snapshots: dict[str, dict[UUID, ChatMessageRecord]] = {}
        self.allowed = True
        self.visible = True
        self.page_reads = 0
        self.after_reads = 0
        self.projections = 0
        self.revision_reads = 0
        self.read_marks: list[datetime] = []

    @property
    def url(self) -> str:
        return f"/api/agents/{self.agent.id}/sessions/{self.session.id}/messages/page"

    def message(self, index: int) -> ChatMessageRecord:
        return ChatMessageRecord(
            id=UUID(int=index),
            agent_id=self.agent.id,
            user_id=self.viewer.id,
            role="assistant",
            content=f"message {index}",
            conversation_id=str(self.session.id),
            created_at=self.timestamp,
        )

    async def access(self, _user: UserRecord, _agent: UUID) -> tuple[AgentRecord, str]:
        if not self.allowed:
            raise HTTPException(403, "Not authorized")
        return self.agent, "use"

    async def session_for_agent(self, session_id: UUID, agent_id: UUID) -> ChatSessionRecord | None:
        if self.visible and session_id == self.session.id and agent_id in (self.agent.id, self.session.peer_agent_id):
            return self.session
        return None

    async def current_revision(self, _session_id: UUID) -> str | None:
        self.revision_reads += 1
        if self.revision is not None:
            self.snapshots[self.revision] = {row.id: replace(row) for row in self.rows}
        return self.revision

    async def page(self, *, conversation_id: str, limit: int, before: tuple[datetime, UUID] | None = None):
        self.page_reads += 1
        rows = [row for row in self.rows if row.conversation_id == conversation_id]
        ordered = sorted(rows, key=self.position, reverse=True)
        return [row for row in ordered if before is None or self.position(row) < before][:limit]

    async def after(self, *, conversation_id: str, after: tuple[datetime, UUID], limit: int):
        self.after_reads += 1
        rows = [row for row in self.rows if row.conversation_id == conversation_id and self.position(row) >= after]
        return sorted(rows, key=self.position)[:limit]

    async def changes(self, *, session_id: UUID, revision: str, since: str, limit: int):
        previous = self.snapshots.get(since)
        if previous is None or set(previous) - {row.id for row in self.rows}:
            return None
        changed = [row for row in self.rows if row != previous.get(row.id)]
        return changed if len(changed) <= limit else None

    def held(self, row: ChatMessageRecord, revision: str | None = None) -> dict[str, str]:
        """Query parameters of a client whose newest held message is ``row`` at ``revision``."""
        if self.revision is not None:
            self.snapshots[self.revision] = {message.id: replace(message) for message in self.rows}
        held_revision = revision or self.revision
        if held_revision is None:
            raise ValueError("Fixture requires a held revision")
        return {
            "revision": held_revision,
            "after_created_at": self.position(row)[0].isoformat(),
            "after_message_id": str(row.id),
        }

    @staticmethod
    def position(row: ChatMessageRecord) -> tuple[datetime, UUID]:
        if row.created_at is None:
            raise ValueError("Fixture requires a timestamp")
        return row.created_at, row.id

    async def project(self, messages: Sequence[ChatMessageRecord], source: str):
        self.projections += 1
        return await project_messages(messages, source)

    async def mark_read(self, *, db_obj: ChatSessionRecord, obj_in: dict[str, datetime]) -> ChatSessionRecord:
        self.read_marks.append(obj_in["last_read_at_by_user"])
        return db_obj

    async def names(self, _ids: Sequence[UUID]) -> dict[str, str]:
        return {}


@pytest.fixture
async def history_http(
    monkeypatch: pytest.MonkeyPatch, fake_cache: FakeRedis
) -> AsyncIterator[tuple[HistoryHarness, AsyncClient]]:
    harness = HistoryHarness(fake_cache)
    monkeypatch.setattr(chat_sessions, "check_agent_access", harness.access)
    monkeypatch.setattr(chat_session_dao, "get_for_agent_or_peer", harness.session_for_agent)
    monkeypatch.setattr(chat_session_dao, "update", harness.mark_read)
    monkeypatch.setattr(chat_message_dao, "history_revision", harness.current_revision)
    monkeypatch.setattr(chat_message_dao, "list_session_page", harness.page)
    monkeypatch.setattr(chat_message_dao, "list_session_after", harness.after)
    monkeypatch.setattr(chat_message_dao, "list_session_changes", harness.changes)
    monkeypatch.setattr(chat_message_dao, "sender_names", harness.names)
    monkeypatch.setattr(chat_history, "project_messages", harness.project)
    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = lambda: harness.viewer
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://history.test") as client:
        yield harness, client
