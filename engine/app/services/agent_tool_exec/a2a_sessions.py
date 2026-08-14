from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_session_dao
from app.dao.participant_dao import participant_dao

if TYPE_CHECKING:
    from app.records.agent import AgentRecord
    from app.records.chat import ChatSessionRecord


async def _resolve_a2a_target(
    db: object | None,
    from_agent_id: uuid.UUID,
    agent_name: str,
) -> tuple[AgentRecord | None, str | None]:
    """Resolve a peer agent by exact then fuzzy name match within the source tenant.

    ``db`` is accepted for dual-stack call-site compatibility and ignored.
    """
    del db
    source_agent = await agent_dao.get(from_agent_id)
    source_tenant_id = source_agent.tenant_id if source_agent else None

    if source_tenant_id:
        exact = await agent_dao.list_by_names_for_tenant(source_tenant_id, [agent_name], exclude_stopped=False)
        for agent in exact:
            if agent.id != from_agent_id:
                return agent, None
        candidates = await agent_dao.list_for_tenant(source_tenant_id)
        safe_name = agent_name.replace("%", "").replace("_", "").casefold()
        for agent in candidates:
            if agent.id == from_agent_id:
                continue
            if safe_name and safe_name in (agent.name or "").casefold():
                return agent, None
    else:
        matches = await agent_dao.list_by_name_any(agent_name, exclude_stopped=False)
        for agent in matches:
            if agent.id != from_agent_id:
                return agent, None

    rels = await agent_agent_relationship_dao.list_for_agent_with_targets(from_agent_id)
    rel_names = [r.target_agent.name for r in rels if r.target_agent]
    return (
        None,
        f"❌ No agent found matching '{agent_name}'. Your connected colleagues: "
        + f"{', '.join(rel_names) if rel_names else 'none - ask your administrator to set up relationships'}",
    )


async def _ensure_a2a_session(
    db: object | None,
    from_agent_id: uuid.UUID,
    target_id: uuid.UUID,
    source_name: str,
    owner_id: uuid.UUID,
) -> tuple[ChatSessionRecord, str]:
    """Get or create the canonical agent↔agent chat session.

    ``db`` is accepted for dual-stack call-site compatibility and ignored.
    """
    del db
    session_agent_id = min(from_agent_id, target_id, key=str)
    session_peer_id = max(from_agent_id, target_id, key=str)
    chat_session = await chat_session_dao.get_agent_peer_session(
        session_agent_id=session_agent_id,
        peer_agent_id=session_peer_id,
    )
    if not chat_session:
        src_participant = await participant_dao.get_by_type_ref("agent", from_agent_id)
        src_part_id = src_participant.id if src_participant else None
        target = await agent_dao.get(target_id)
        target_name = target.name if target else "Unknown"
        chat_session = await chat_session_dao.create(
            obj_in={
                "agent_id": session_agent_id,
                "user_id": owner_id,
                "title": f"{source_name} ↔ {target_name}",
                "source_channel": "agent",
                "participant_id": src_part_id,
                "peer_agent_id": session_peer_id,
            }
        )
    return chat_session, str(chat_session.id)
