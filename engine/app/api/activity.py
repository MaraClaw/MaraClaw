"""Activity log API - view agent work history."""

import re
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.json_types import int_from_row
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao.activity_log_dao import agent_activity_log_dao
from app.dao.agent_dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.participant_dao import participant_dao
from app.dao.user_dao import user_dao
from app.records.user import UserRecord

router = APIRouter(tags=["activity"])


def _group_key(row: dict[str, object]) -> str:
    value = row.get("group_key")
    return str(value) if value is not None else ""


def _group_last_iso(row: dict[str, object]) -> str | None:
    last_at = row.get("last_at")
    return last_at.isoformat() if isinstance(last_at, datetime) else None


def _group_count(row: dict[str, object]) -> int:
    return int_from_row(row.get("cnt"))


@router.get("/agents/{agent_id}/activity")
async def get_agent_activity(
    agent_id: uuid.UUID, limit: int = Query(50, le=200), current_user: UserRecord = Depends(get_current_user)
) -> list[dict[str, Any]]:
    """Get recent activity logs for an agent."""
    _ = await check_agent_access(current_user, agent_id)

    logs = await agent_activity_log_dao.list_for_agent(agent_id, limit=limit)

    return [
        {
            "id": str(log.id),
            "action_type": log.action_type,
            "summary": log.summary,
            "detail": log.detail_json,
            "related_id": str(log.related_id) if log.related_id else None,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]


# ─── Chat History (per-agent) ─────────────────────────────────


@router.get("/agents/{agent_id}/chat-history/conversations")
async def list_conversations(agent_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)) -> list[dict[str, Any]]:
    """List all conversation partners for this agent (web users + other agents)."""
    _ = await check_agent_access(current_user, agent_id)

    conversations: list[dict[str, Any]] = []

    # 1. Web chat conversations (from ChatMessage table, grouped by user)
    web_groups = await chat_message_dao.conversation_groups_for_agent(agent_id, group_by_user=True)
    web_user_ids = [key for key in (row["group_key"] for row in web_groups) if isinstance(key, UUID)]
    web_names = await user_dao.display_names_for_ids(web_user_ids)
    web_latest = await chat_message_dao.latest_contents(agent_id=agent_id, user_ids=web_user_ids)
    for row in web_groups:
        user_id = row.get("group_key")
        name = web_names.get(user_id) if isinstance(user_id, UUID) else None
        name = name or "Unknown user"
        last_content = web_latest.get(str(user_id), "")
        conversations.append(
            {
                "conv_id": f"web_{user_id}",
                "partner_type": "user",
                "partner_id": str(user_id) if user_id is not None else "",
                "partner_name": f"👤 {name}",
                "last_message": last_content[:80],
                "message_count": _group_count(row),
                "last_at": _group_last_iso(row),
            }
        )

    # 1b. Feishu conversations (P2P and group)
    feishu_groups = await chat_message_dao.conversation_groups_for_agent(agent_id, conversation_prefix="feishu_")
    feishu_ids = [_group_key(row) for row in feishu_groups]
    feishu_latest = await chat_message_dao.latest_contents(agent_id=agent_id, conversation_ids=feishu_ids)
    p2p_ids = [cid for cid in feishu_ids if cid.startswith("feishu_p2p_")]
    feishu_first = await chat_message_dao.latest_contents(
        agent_id=agent_id,
        conversation_ids=p2p_ids,
        role="user",
        ascending=True,
    )
    for row in feishu_groups:
        conv_id = _group_key(row)
        last_content = feishu_latest.get(conv_id, "")

        if conv_id.startswith("feishu_p2p_"):
            first_msg = feishu_first.get(conv_id, "")
            sender_match = re.search(r"\[发送者:\s*([^\]]+?)(?:\s*\(ID:.*?\))?\]", first_msg)
            display_name = f"📱 {sender_match.group(1)}" if sender_match else "📱 Feishu user"
        else:
            display_name = "👥 Feishu group chat"

        conversations.append(
            {
                "conv_id": conv_id,
                "partner_type": "feishu",
                "partner_id": conv_id,
                "partner_name": display_name,
                "last_message": last_content[:80],
                "message_count": _group_count(row),
                "last_at": _group_last_iso(row),
            }
        )

    # 1c. Slack / Discord conversations
    for prefix, icon, label in [("slack_", "💬", "Slack"), ("discord_", "🎮", "Discord")]:
        ch_groups = await chat_message_dao.conversation_groups_for_agent(agent_id, conversation_prefix=prefix)
        ch_ids = [_group_key(row) for row in ch_groups]
        ch_latest = await chat_message_dao.latest_contents(agent_id=agent_id, conversation_ids=ch_ids)
        for row in ch_groups:
            conv_id = _group_key(row)
            last_content = ch_latest.get(conv_id, "")
            parts = conv_id.split("_", 2)
            channel_part = parts[1] if len(parts) > 1 else conv_id
            display_name = f"{icon} {label} #{channel_part}" if channel_part != "dm" else f"{icon} {label} DM"
            conversations.append(
                {
                    "conv_id": conv_id,
                    "partner_type": prefix.rstrip("_"),
                    "partner_id": conv_id,
                    "partner_name": display_name,
                    "last_message": last_content[:80],
                    "message_count": _group_count(row),
                    "last_at": _group_last_iso(row),
                }
            )

    # 2. Agent-to-agent conversations
    agent_sessions = await chat_session_dao.list_agent_peer_sessions_for_agent(agent_id)
    partner_ids = []
    session_ids = []
    for sess in agent_sessions:
        partner_id = sess.peer_agent_id if sess.agent_id == agent_id else sess.agent_id
        if partner_id:
            partner_ids.append(partner_id)
        session_ids.append(str(sess.id))
    partners = {row.id: row for row in await agent_dao.get_many(partner_ids)}
    session_stats = await chat_message_dao.message_stats_for_conversations(session_ids)
    session_latest = await chat_message_dao.list_latest_for_conversations(
        conversation_ids=session_ids,
        limit=1,
    )
    for sess in agent_sessions:
        partner_id = sess.peer_agent_id if sess.agent_id == agent_id else sess.agent_id
        partner = partners.get(partner_id) if partner_id else None
        partner_name = partner.name if partner else "Unknown digital employee"
        sid = str(sess.id)
        cnt, last_at = session_stats.get(sid, (0, None))
        last_msgs = session_latest.get(sid) or []
        last_content = last_msgs[0].content if last_msgs else ""

        conversations.append(
            {
                "conv_id": sid,
                "partner_type": "agent",
                "partner_id": str(partner_id),
                "partner_name": f"🤖 {partner_name}",
                "last_message": last_content[:80],
                "message_count": cnt,
                "last_at": last_at.isoformat() if last_at else None,
            }
        )

    conversations.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return conversations


@router.get("/agents/{agent_id}/chat-history/{conv_id:path}")
async def get_conversation_messages(
    agent_id: uuid.UUID,
    conv_id: str,
    limit: int = Query(100, le=500),
    current_user: UserRecord = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Get messages for a specific conversation."""
    _ = await check_agent_access(current_user, agent_id)

    messages: list[dict[str, Any]] = []

    if conv_id.startswith(("web_", "feishu_", "slack_", "discord_")):
        rows = await chat_message_dao.list_for_agent_conversation(
            agent_id=agent_id,
            conversation_id=conv_id,
            limit=limit,
            ascending=True,
        )
        for m in rows:
            content = m.content
            if content.startswith("[发送者:"):
                content = re.sub(r"^\[发送者:[^\]]*\]\s*", "", content)
            messages.append(
                {
                    "id": str(m.id),
                    "role": m.role,
                    "content": content,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
            )
    elif conv_id.startswith("agent_") or len(conv_id) == 36:
        rows = await chat_message_dao.list_for_session(conversation_id=conv_id, limit=limit)
        name_cache: dict[str, str] = {}
        for m in rows:
            sender_name = "Unknown"
            if m.participant_id:
                pid_str = str(m.participant_id)
                if pid_str not in name_cache:
                    participant = await participant_dao.get(m.participant_id)
                    name_cache[pid_str] = (participant.display_name if participant else None) or "Unknown"
                sender_name = name_cache[pid_str]
            messages.append(
                {
                    "id": str(m.id),
                    "role": m.role,
                    "sender_name": sender_name,
                    "content": m.content,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
            )

    return messages
