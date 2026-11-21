from collections.abc import Callable
from uuid import UUID, uuid5

from fastapi import HTTPException

from app.dao import agent_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.shared_chat_dao import insert_shared_session
from app.records.chat import ChatSessionRecord

SHARED_CHAT_NAMESPACE = UUID("7ac3f6c5-693a-49c4-901d-4c1ca0f07ef8")


def shared_session_id(agent_id: UUID) -> UUID:
    return uuid5(SHARED_CHAT_NAMESPACE, f"agent:{agent_id}:shared-human:v1")


def is_shared_session(session: ChatSessionRecord) -> bool:
    return session.source_channel == "shared" and session.id == shared_session_id(session.agent_id)


def is_canonical(session: ChatSessionRecord) -> bool:
    """The (agent, user) conversation row: the terms of `uq_chat_sessions_primary_platform`."""
    return session.source_channel == "web" and session.is_primary and not session.is_group


async def ensure_canonical_session(
    agent_id: UUID, user_id: UUID, *, guard: Callable[[], None] | None = None
) -> ChatSessionRecord:
    """Return the user's canonical conversation with the agent, creating it on first use.

    The insert takes foreign-key locks on the agent and the user. Call it before locking a chat session row of the
    agent, and never while holding FOR UPDATE on the tenant, user or agent row.
    """
    agent = await agent_dao.get(agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    session = await chat_session_dao.insert_primary_platform(agent_id, user_id, guard=guard)
    if session.agent_id != agent_id or session.user_id != user_id or not is_canonical(session):
        raise HTTPException(409, "Canonical session identity conflict")
    return session


async def ensure_shared_session(agent_id: UUID, *, guard: Callable[[], None] | None = None) -> ChatSessionRecord:
    agent = await agent_dao.get(agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    session = await insert_shared_session(shared_session_id(agent_id), agent_id, agent.creator_id, guard=guard)
    if session.agent_id != agent_id or not is_shared_session(session):
        raise HTTPException(409, "Canonical shared session identity conflict")
    return session


class SessionNotWritableError(Exception):
    """The user may read the session but not write to it; their own canonical session is where they can write."""

    agent_id: UUID
    user_id: UUID

    def __init__(self, agent_id: UUID, user_id: UUID) -> None:
        super().__init__(f"Session is read-only for user {user_id} on agent {agent_id}")
        self.agent_id = agent_id
        self.user_id = user_id


def read_only_detail(session_id: UUID) -> dict[str, str]:
    return {"reason": "session_read_only", "session_id": str(session_id)}


async def read_only_refusal(refused: SessionNotWritableError) -> HTTPException:
    """The 409 for a refused write; resolves the caller's canonical session on its own short connection."""
    canonical = await ensure_canonical_session(refused.agent_id, refused.user_id)
    return HTTPException(409, read_only_detail(canonical.id))


def require_writable_session(session: ChatSessionRecord, user_id: UUID) -> None:
    if session.is_group or session.source_channel == "agent" or (is_canonical(session) and session.user_id == user_id):
        return
    raise SessionNotWritableError(session.agent_id, user_id)
