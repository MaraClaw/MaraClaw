"""Session visibility after the caller has authorized access to the agent."""

from app.core.domain.users import UserRole
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord


def can_view_all_agent_chat_sessions(user: UserRecord, agent: AgentRecord) -> bool:
    """Only company admins may list/view/delete other users' chat sessions."""
    del agent
    return user.role == UserRole.ORG_ADMIN


def can_access_chat_session(user: UserRecord, agent: AgentRecord, session: ChatSessionRecord) -> bool:
    return str(session.user_id) == str(user.id) or can_view_all_agent_chat_sessions(user, agent)
