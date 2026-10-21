from uuid import UUID

from fastapi import HTTPException, WebSocket

from app.services import chat_tickets
from app.services.shared_chat import shared_session_id


def is_shared_target(agent_id: str, session_id: str | None) -> bool:
    try:
        return UUID(session_id or "") == shared_session_id(UUID(agent_id))
    except ValueError:
        return False


async def authorize_chat_delivery(websocket: WebSocket, agent_id: str, session_id: str | None) -> bool:
    if not is_shared_target(agent_id, session_id):
        return True
    binding = getattr(websocket.state, "chat_binding", None)
    if not isinstance(binding, chat_tickets.ChatTicketBinding):
        return False
    if str(binding.agent_id) != agent_id or str(binding.session_id) != session_id:
        return False
    try:
        await chat_tickets.authorize(binding)
        await chat_tickets.authorized_session(binding)
    except HTTPException:
        return False
    return True
