from collections.abc import Callable
from uuid import UUID

from app.db.session import connection_ctx
from app.records.chat import ChatSessionRecord


async def insert_shared_session(
    session_id: UUID, agent_id: UUID, creator_id: UUID, *, guard: Callable[[], None] | None = None
) -> ChatSessionRecord:
    async with connection_ctx() as conn:
        if guard is not None:
            guard()
        await conn.execute(
            "INSERT INTO chat_sessions (id, agent_id, user_id, title, source_channel, is_primary) "
            "VALUES (%(id)s, %(agent_id)s, %(creator_id)s, 'Shared conversation', 'shared', TRUE) "
            "ON CONFLICT (id) DO NOTHING",
            {"id": session_id, "agent_id": agent_id, "creator_id": creator_id},
        )
        row = await conn.fetchone("SELECT * FROM chat_sessions WHERE id = %(id)s", {"id": session_id})
        if row is None:
            raise RuntimeError("Shared session disappeared during creation")
        return ChatSessionRecord.from_row(row)
