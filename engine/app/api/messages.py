"""Messages API - inbox, unread count, mark as read.

After the Participant abstraction migration, agent-to-agent messages are stored
in chat_messages (via ChatSession with source_channel='agent').
This API now queries chat_sessions + chat_messages for the inbox.
"""

from fastapi import APIRouter, Depends, Query

from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.participant_dao import participant_dao
from app.records.user import UserRecord

router = APIRouter(tags=["messages"])


@router.get("/messages/inbox")
async def get_inbox(limit: int = Query(50, le=200), current_user: UserRecord = Depends(get_current_user)):
    """Get agent-to-agent messages for agents the current user manages.

    Returns recent messages from ChatSessions with source_channel='agent'
    where the user's agents are participants.
    """
    my_agent_ids = list(await agent_dao.list_ids_for_creator(current_user.id))

    if not my_agent_ids:
        return []

    sessions = await chat_session_dao.list_agent_channel_sessions(agent_ids=my_agent_ids, limit=limit)
    conversation_ids = [str(sess.id) for sess in sessions]
    messages_by_conv = await chat_message_dao.list_latest_for_conversations(
        conversation_ids=conversation_ids,
        limit=3,
    )
    participant_ids = {msg.participant_id for msgs in messages_by_conv.values() for msg in msgs if msg.participant_id}
    participants = {
        participant.id: participant for participant in await participant_dao.get_many(list(participant_ids))
    }

    result_list = []
    for sess in sessions:
        for msg in messages_by_conv.get(str(sess.id), []):
            sender_name = "Unknown"
            if msg.participant_id:
                participant = participants.get(msg.participant_id)
                sender_name = (participant.display_name if participant else None) or "Unknown"

            result_list.append(
                {
                    "id": str(msg.id),
                    "sender_type": "agent",
                    "sender_name": sender_name,
                    "content": msg.content,
                    "session_title": sess.title,
                    "created_at": msg.created_at.isoformat() if msg.created_at else None,
                }
            )

    result_list.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return result_list[:limit]


@router.get("/messages/unread-count")
async def get_unread_count(current_user: UserRecord = Depends(get_current_user)):
    """Get count of unread agent-to-agent messages for the current user's agents."""
    my_agent_ids = await agent_dao.list_ids_for_creator(current_user.id)

    if not my_agent_ids:
        return {"unread_count": 0}

    # Count agent-to-agent sessions with recent activity
    # (Since we don't have per-message read tracking on ChatMessage yet,
    # just return 0 for now - this can be enhanced later)
    return {"unread_count": 0}
