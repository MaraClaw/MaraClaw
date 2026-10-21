from collections.abc import Callable
from uuid import UUID, uuid5

from fastapi import HTTPException

from app.dao import agent_dao
from app.dao.shared_chat_dao import insert_shared_session
from app.records.chat import ChatSessionRecord

SHARED_CHAT_NAMESPACE = UUID("7ac3f6c5-693a-49c4-901d-4c1ca0f07ef8")


def shared_session_id(agent_id: UUID) -> UUID:
    return uuid5(SHARED_CHAT_NAMESPACE, f"agent:{agent_id}:shared-human:v1")


def is_shared_session(session: ChatSessionRecord) -> bool:
    return session.source_channel == "shared" and session.id == shared_session_id(session.agent_id)


async def ensure_shared_session(agent_id: UUID, *, guard: Callable[[], None] | None = None) -> ChatSessionRecord:
    agent = await agent_dao.get(agent_id)
    if agent is None:
        raise HTTPException(404, "Agent not found")
    session = await insert_shared_session(shared_session_id(agent_id), agent_id, agent.creator_id, guard=guard)
    if session.agent_id != agent_id or not is_shared_session(session):
        raise HTTPException(409, "Canonical shared session identity conflict")
    return session


def require_writable_session(session: ChatSessionRecord) -> None:
    if not is_shared_session(session) and not session.is_group and session.source_channel != "agent":
        raise HTTPException(
            409,
            {"reason": "session_archived", "session_id": str(shared_session_id(session.agent_id))},
        )
