"""Session visibility after the caller has authorized access to the agent."""

from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord
from app.services.shared_chat import is_shared_session


def can_view_all_agent_chat_sessions(user: UserRecord, agent: AgentRecord) -> bool:
    """Admins and the agent creator may list/view/delete other users' chat sessions."""
    return user.role in ("platform_admin", "org_admin", "agent_admin") or str(agent.creator_id) == str(user.id)


def can_access_chat_session(user: UserRecord, agent: AgentRecord, session: ChatSessionRecord) -> bool:
    return (
        is_shared_session(session)
        or str(session.user_id) == str(user.id)
        or can_view_all_agent_chat_sessions(user, agent)
    )
