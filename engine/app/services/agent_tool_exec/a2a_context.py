from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.logging import logger
from app.core.permissions import evaluate_agent_relationship_status
from app.dao.agent_agent_relationship_dao import agent_agent_relationship_dao
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.llm_dao import llm_model_dao
from app.dao.participant_dao import participant_dao
from app.dao.tenant_dao import tenant_dao
from app.services.agent_tool_exec.registry import ToolArguments
from app.services.llm.types import OpenAIMessage


@dataclass
class A2AContext:
    source_agent: Any
    target_agent: Any
    chat_session_id: str
    session_agent_id: uuid.UUID
    owner_id: uuid.UUID
    src_participant_id: uuid.UUID | None
    tgt_participant_id: uuid.UUID | None
    msg_type: str
    message_text: str
    origin_source_channel: str
    origin_session_id: str | None
    primary_model: Any | None = None
    fallback_model: Any | None = None
    conversation_history: list[OpenAIMessage] = field(default_factory=list)


async def _resolve_target_agent(
    *,
    from_agent_id: uuid.UUID,
    agent_name: str,
    source_tenant_id: uuid.UUID | None,
) -> Any | None:
    """Exact name match first, then fuzzy ILIKE-style contains match within tenant."""
    if source_tenant_id:
        exact = await agent_dao.list_by_names_for_tenant(source_tenant_id, [agent_name], exclude_stopped=False)
        for agent in exact:
            if agent.id != from_agent_id:
                return agent
        candidates = await agent_dao.list_for_tenant(source_tenant_id)
        safe_name = agent_name.replace("%", "").replace("_", "").casefold()
        for agent in candidates:
            if agent.id == from_agent_id:
                continue
            if safe_name and safe_name in (agent.name or "").casefold():
                return agent
        return None

    matches = await agent_dao.list_by_name_any(agent_name, exclude_stopped=False)
    for agent in matches:
        if agent.id != from_agent_id:
            return agent
    return None


async def _build_a2a_context(
    from_agent_id: uuid.UUID,
    args: ToolArguments,
    user_id: uuid.UUID | None = None,
    origin_session_id: str | None = None,
) -> A2AContext | str:
    agent_name = args.get("agent_name", "")
    message_text = args.get("message", "")
    msg_type = args.get("msg_type", "notify")
    agent_name = agent_name.strip() if isinstance(agent_name, str) else ""
    message_text = message_text.strip() if isinstance(message_text, str) else ""
    msg_type = msg_type.strip().lower() if isinstance(msg_type, str) else "notify"
    force_async = bool(args.get("force_async"))

    if not agent_name or not message_text:
        return "❌ Please provide target agent name and message content"
    try:
        origin_source_channel = "web"
        if origin_session_id:
            with suppress(Exception):
                origin_sess = await chat_session_dao.get(uuid.UUID(origin_session_id))
                if origin_sess:
                    origin_source_channel = origin_sess.source_channel

        source_agent = await agent_dao.get(from_agent_id)
        if not source_agent:
            return "❌ Source agent not found"
        source_name = source_agent.name
        source_tenant_id = source_agent.tenant_id
        owner_id = user_id or source_agent.creator_id

        target = await _resolve_target_agent(
            from_agent_id=from_agent_id,
            agent_name=agent_name,
            source_tenant_id=source_tenant_id,
        )
        if not target:
            rels = await agent_agent_relationship_dao.list_for_agent_with_targets(from_agent_id)
            rel_names = [r.target_agent.name for r in rels if r.target_agent]
            return (
                f"❌ No agent found matching '{agent_name}'. Your connected colleagues: "
                f"{', '.join(rel_names) if rel_names else 'none — ask your administrator to set up relationships'}"
            )

        if target.is_expired or (target.expires_at and datetime.now(UTC) >= target.expires_at):
            return (
                f"⚠️ {target.name} is currently unavailable — their service period has ended. "
                f"Please contact the platform administrator."
            )

        rels = await agent_agent_relationship_dao.list_for_agent(from_agent_id)
        rel = next((r for r in rels if r.target_agent_id == target.id), None)
        if not rel:
            return (
                f"❌ You do not have a relationship with {target.name}. Only agents in your relationship list "
                f"can be contacted. Ask your administrator to add a relationship if needed."
            )
        status_info = await evaluate_agent_relationship_status(None, rel)
        if status_info["access_status"] != "active":
            return (
                f"❌ Relationship to {target.name} is not active "
                f"({status_info['access_status_reason'] or 'restricted'}). "
                f"Ask a manager of both agents to review Relationships."
            )

        src_participant = await participant_dao.get_by_type_ref("agent", from_agent_id)
        src_participant_id = src_participant.id if src_participant else None
        tgt_participant = await participant_dao.get_by_type_ref("agent", target.id)
        tgt_participant_id = tgt_participant.id if tgt_participant else None

        session_agent_id = min(from_agent_id, target.id, key=str)
        session_peer_id = max(from_agent_id, target.id, key=str)
        chat_session = await chat_session_dao.get_agent_peer_session(
            session_agent_id=session_agent_id,
            peer_agent_id=session_peer_id,
        )
        if not chat_session:
            chat_session = await chat_session_dao.create(
                obj_in={
                    "agent_id": session_agent_id,
                    "user_id": owner_id,
                    "title": f"{source_name} ↔ {target.name}",
                    "source_channel": "agent",
                    "participant_id": src_participant_id,
                    "peer_agent_id": session_peer_id,
                }
            )

        session_id = str(chat_session.id)
        await chat_message_dao.insert_message(
            agent_id=session_agent_id,
            user_id=owner_id,
            role="user",
            content=message_text,
            conversation_id=session_id,
            participant_id=src_participant_id,
        )
        await chat_session_dao.update(
            db_obj=chat_session,
            obj_in={"last_message_at": datetime.now(UTC)},
        )

        if getattr(target, "agent_type", "native") == "openclaw":
            return A2AContext(
                source_agent=source_agent,
                target_agent=target,
                chat_session_id=session_id,
                session_agent_id=session_agent_id,
                owner_id=owner_id,
                src_participant_id=src_participant_id,
                tgt_participant_id=tgt_participant_id,
                msg_type=msg_type,
                message_text=message_text,
                origin_source_channel=origin_source_channel,
                origin_session_id=origin_session_id,
            )

        a2a_async = False
        if source_tenant_id:
            with suppress(Exception):
                tenant = await tenant_dao.get(source_tenant_id)
                if tenant:
                    a2a_async = bool(getattr(tenant, "a2a_async_enabled", False))
        if not a2a_async and not force_async and msg_type in ("notify", "task_delegate"):
            msg_type = "consult"

        primary_model = None
        fallback_model = None
        conversation_history: list[OpenAIMessage] = []

        if msg_type == "consult":
            if target.primary_model_id:
                primary_model = await llm_model_dao.get(target.primary_model_id)
            if target.fallback_model_id:
                fallback_model = await llm_model_dao.get(target.fallback_model_id)
            if not primary_model and not fallback_model:
                return f"⚠️ {target.name} has no LLM model configured"

            hist = await chat_message_dao.list_recent(
                agent_id=session_agent_id,
                conversation_id=session_id,
                limit=20,
            )
            for message in hist:
                if message.participant_id and src_participant_id and message.participant_id == src_participant_id:
                    role: Literal["user", "assistant"] = "user"
                else:
                    role = "assistant"
                conversation_history.append({"role": role, "content": message.content})

        return A2AContext(
            source_agent=source_agent,
            target_agent=target,
            chat_session_id=session_id,
            session_agent_id=session_agent_id,
            owner_id=owner_id,
            src_participant_id=src_participant_id,
            tgt_participant_id=tgt_participant_id,
            msg_type=msg_type,
            message_text=message_text,
            origin_source_channel=origin_source_channel,
            origin_session_id=origin_session_id,
            primary_model=primary_model,
            fallback_model=fallback_model,
            conversation_history=conversation_history,
        )
    except Exception as error:
        logger.exception(f"[A2A] _build_a2a_context failed: from={from_agent_id}")
        return f"❌ A2A context error ({type(error).__name__}): {str(error)[:200]}"
