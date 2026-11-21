"""Which conversation a gateway row belongs to, and whether that conversation is a human lane.

A human lane is a canonical per-user session (``shared_chat.is_canonical``). Classification is ONE unlocked read:
nothing updates ``chat_sessions.is_primary`` after insert, and ``lock_human_lane`` re-checks the predicate under
FOR UPDATE, so a misclassified id finds no lane and never claims work.
"""

from uuid import UUID

from app.dao.chat_dao import chat_session_dao
from app.records.chat import ChatSessionRecord
from app.services.shared_chat import is_canonical


async def conversation_session(agent_id: UUID, conversation_id: str | None) -> ChatSessionRecord | None:
    """The agent's chat session named by a gateway ``conversation_id``; ``None`` for a non-session id."""
    if conversation_id is None:
        return None
    try:
        session_id = UUID(conversation_id)
    except ValueError:
        return None
    return await chat_session_dao.get_for_agent(session_id, agent_id)


async def human_lane_session(agent_id: UUID, conversation_id: str | None) -> ChatSessionRecord | None:
    """The canonical session whose FIFO human lane ``conversation_id`` names, else ``None`` (an ordinary lane)."""
    session = await conversation_session(agent_id, conversation_id)
    return session if session is not None and is_canonical(session) else None
