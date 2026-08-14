"""Chat session management API endpoints."""

import json
import re
import uuid
from datetime import UTC, datetime
from typing import ClassVar, NotRequired, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.core.json_types import JsonValue
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao import agent_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.user import UserRecord

router = APIRouter(prefix="/api/agents", tags=["chat-sessions"])


class ChatMessageEntry(TypedDict):
    role: str
    content: str
    created_at: NotRequired[str | None]
    toolName: NotRequired[str]
    toolArgs: NotRequired[JsonValue]
    toolStatus: NotRequired[str]
    toolResult: NotRequired[str]
    toolThinking: NotRequired[str]
    thinking: NotRequired[str]
    sender_name: NotRequired[str]
    participant_id: NotRequired[str]


def _can_view_all_agent_chat_sessions(user: UserRecord, agent: AgentRecord) -> bool:
    """Admins and the agent creator may list/view/delete other users' chat sessions."""
    return user.role in ("platform_admin", "org_admin", "agent_admin") or str(agent.creator_id) == str(user.id)


class SessionOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    user_id: str
    username: str | None = None  # display_name ?? username
    source_channel: str = "web"  # web / feishu / discord / slack / agent
    title: str
    created_at: str
    last_message_at: str | None = None
    message_count: int = 0
    unread_count: int = 0
    is_primary: bool = False
    # Agent-to-agent session fields
    peer_agent_id: str | None = None
    peer_agent_name: str | None = None
    participant_type: str = "user"  # 'user' | 'agent'
    # Group chat session fields
    is_group: bool = False
    group_name: str | None = None


class CreateSessionIn(BaseModel):
    title: str | None = None


class PatchSessionIn(BaseModel):
    title: str


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: str = Query("mine", description="'mine' or 'all'"),
    current_user: UserRecord = Depends(get_current_user),
) -> list[SessionOut]:
    """List chat sessions for an agent. scope=all for org/platform admins and agent_admin."""
    agent = await agent_dao.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    _ = await check_agent_access(current_user, agent_id)

    if scope == "all":
        if not _can_view_all_agent_chat_sessions(current_user, agent):
            raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

        sessions = list(await chat_session_dao.list_all_for_agent(agent_id))
        out: list[SessionOut] = []

        session_ids = [str(s.id) for s in sessions]
        session_uuid_ids = [s.id for s in sessions]
        message_counts = await chat_session_dao.message_counts(session_ids)
        unread_counts = await chat_session_dao.unread_counts_for_user(
            session_ids=session_uuid_ids,
            user_id=current_user.id,
            mine_only=False,
        )

        user_ids = list({s.user_id for s in sessions if not s.is_group and s.source_channel != "agent" and s.user_id})
        user_names: dict[str, str] = {}
        if user_ids:
            async with connection_ctx() as conn:
                rows = await conn.fetchall(
                    "SELECT u.id, COALESCE(u.display_name, i.username) AS display "
                    + "FROM users u JOIN identities i ON u.identity_id = i.id "
                    + "WHERE u.id = ANY(%(ids)s)",
                    {"ids": user_ids},
                )
            for row in rows:
                user_names[str(row["id"])] = row["display"] or "Unknown"

        agent_ids_to_fetch: set[uuid.UUID] = set()
        for s in sessions:
            if s.source_channel == "agent" and s.peer_agent_id:
                agent_ids_to_fetch.add(s.agent_id)
                agent_ids_to_fetch.add(s.peer_agent_id)
        agent_names: dict[str, str] = {}
        if agent_ids_to_fetch:
            async with connection_ctx() as conn:
                rows = await conn.fetchall(
                    "SELECT id, name FROM agents WHERE id = ANY(%(ids)s)",
                    {"ids": list(agent_ids_to_fetch)},
                )
            for row in rows:
                agent_names[str(row["id"])] = row["name"] or "Agent"

        for session in sessions:
            count = message_counts.get(str(session.id), 0)
            if count == 0:
                continue

            display = None
            peer_agent_id = None
            peer_agent_name = None
            participant_type = "user"

            if session.source_channel == "agent" and session.peer_agent_id:
                participant_type = "agent"
                peer_agent_id = str(session.peer_agent_id)
                a1_name = agent_names.get(str(session.agent_id), "Agent")
                a2_name = agent_names.get(str(session.peer_agent_id), "Agent")
                peer_agent_name = a2_name
                display = f"Agent {a1_name} - {a2_name}"
            elif session.is_group:
                display = session.group_name or session.title or "Group Chat"
            else:
                display = user_names.get(str(session.user_id), "Unknown")

            out.append(
                SessionOut(
                    id=str(session.id),
                    agent_id=str(session.agent_id),
                    user_id=str(session.user_id),
                    username=display,
                    source_channel=session.source_channel,
                    title=session.title,
                    created_at=session.created_at.isoformat() if session.created_at else "",
                    last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                    message_count=count,
                    unread_count=unread_counts.get(str(session.id), 0),
                    is_primary=bool(session.is_primary),
                    peer_agent_id=peer_agent_id,
                    peer_agent_name=peer_agent_name,
                    participant_type="group" if session.is_group else participant_type,
                    is_group=session.is_group,
                    group_name=session.group_name,
                )
            )
        return out

    # scope == "mine"
    sessions = list(await chat_session_dao.list_for_user(agent_id=agent_id, user_id=current_user.id))
    out = []
    session_ids = [str(s.id) for s in sessions]
    session_uuid_ids = [s.id for s in sessions]
    total_counts = await chat_session_dao.message_counts(session_ids, agent_id=agent_id)
    unread_counts = await chat_session_dao.unread_counts_for_user(
        session_ids=session_uuid_ids,
        user_id=current_user.id,
        mine_only=True,
    )

    for session in sessions:
        count = total_counts.get(str(session.id), 0)
        if count == 0:
            continue
        out.append(
            SessionOut(
                id=str(session.id),
                agent_id=str(session.agent_id),
                user_id=str(session.user_id),
                source_channel=session.source_channel,
                title=session.title,
                created_at=session.created_at.isoformat() if session.created_at else "",
                last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
                message_count=count,
                unread_count=unread_counts.get(str(session.id), 0),
                is_primary=bool(session.is_primary),
            )
        )
    return out


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID, body: CreateSessionIn = CreateSessionIn(), current_user: UserRecord = Depends(get_current_user)
):
    """Create a new chat session for the current user."""
    _ = await check_agent_access(current_user, agent_id)

    now = datetime.now(UTC)
    session = await chat_session_dao.create(
        obj_in={
            "id": uuid.uuid4(),
            "agent_id": agent_id,
            "user_id": current_user.id,
            "title": body.title or f"Session {now.strftime('%m-%d %H:%M')}",
            "source_channel": "web",
            "is_primary": False,
            "created_at": now,
        }
    )
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat() if session.created_at else now.isoformat(),
        last_message_at=None,
        message_count=0,
        unread_count=0,
        is_primary=False,
        participant_type="user",
        is_group=False,
    )


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID, session_id: uuid.UUID, body: PatchSessionIn, current_user: UserRecord = Depends(get_current_user)
):
    """Rename a session. Owner, agent creator, or admin may rename others' sessions."""
    agent, _ = await check_agent_access(current_user, agent_id)
    session = await chat_session_dao.get_for_agent(session_id, agent_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized")

    session = await chat_session_dao.update(db_obj=session, obj_in={"title": body.title})
    return {"id": str(session.id), "title": session.title}


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(agent_id: uuid.UUID, session_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)):
    """Delete a chat session and its messages. Owner, agent creator, or admin may delete others' sessions."""
    agent, _ = await check_agent_access(current_user, agent_id)
    session = await chat_session_dao.get_for_agent(session_id, agent_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized")

    await chat_message_dao.delete_for_conversation(str(session_id))
    _ = await chat_session_dao.delete(id=session_id)
    return


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=500, description="Number of messages to return"),
    before: str = Query(None, description="Cursor: return messages created before this timestamp (ISO format)"),
    current_user: UserRecord = Depends(get_current_user),
) -> list[ChatMessageEntry]:
    """Get chat messages for a specific session."""
    agent, _ = await check_agent_access(current_user, agent_id)
    session = await chat_session_dao.get_for_agent_or_peer(session_id, agent_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid `before` timestamp format. Use ISO 8601.") from None

    messages = list(
        await chat_message_dao.list_for_session(
            conversation_id=str(session_id),
            limit=limit,
            before=before_dt,
        )
    )

    if (
        str(session.user_id) == str(current_user.id)
        and not session.is_group
        and session.source_channel not in ("agent", "trigger")
    ):
        _ = await chat_session_dao.update(
            db_obj=session,
            obj_in={"last_read_at_by_user": datetime.now(UTC)},
        )

    sender_cache: dict[str, str] = {}
    if session.source_channel == "agent":
        participant_ids = list({m.participant_id for m in messages if m.participant_id})
        if participant_ids:
            async with connection_ctx() as conn:
                rows = await conn.fetchall(
                    "SELECT id, display_name FROM participants WHERE id = ANY(%(ids)s)",
                    {"ids": participant_ids},
                )
            for row in rows:
                sender_cache[str(row["id"])] = row["display_name"] or "Unknown"

    out: list[ChatMessageEntry] = []
    for m in messages:
        sender_name = sender_cache.get(str(m.participant_id)) if m.participant_id else None

        if m.role == "tool_call":
            entry: ChatMessageEntry = {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            try:
                data = json.loads(m.content)
                entry["content"] = ""
                entry["toolName"] = data.get("name") or data.get("tool_name") or ""
                entry["toolArgs"] = data.get("args") or data.get("arguments")
                entry["toolStatus"] = data.get("status", "done")
                entry["toolResult"] = data.get("result", "")
                entry["toolThinking"] = data.get("reasoning_content", "")
            except json.JSONDecodeError, TypeError, AttributeError:
                entry["content"] = m.content
            if sender_name:
                entry["sender_name"] = sender_name
            out.append(entry)
            continue

        if session.source_channel == "agent" and m.role == "assistant" and "```tool_code" in (m.content or ""):
            parts = _split_inline_tools(m.content)
            for part in parts:
                if sender_name:
                    part["sender_name"] = sender_name
                if m.participant_id:
                    part["participant_id"] = str(m.participant_id)
                out.append(part)
        else:
            entry = {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            if m.thinking:
                entry["thinking"] = m.thinking
            if sender_name:
                entry["sender_name"] = sender_name
            if m.participant_id:
                entry["participant_id"] = str(m.participant_id)
            out.append(entry)

    return out


def _split_inline_tools(content: str) -> list[ChatMessageEntry]:
    """Parse assistant content containing inline ```tool_code blocks.

    Splits into alternating text segments and tool_call entries.
    Format: ```tool_code\ntool_name\n``` ```json\n{args}\n```
    """
    # Pattern: ```tool_code\n<name>\n``` optionally followed by ```json\n<args>\n```
    pattern = re.compile(
        r"```tool_code\s*\n\s*(\w+)\s*\n```"  # tool name
        + r"(?:\s*```json\s*\n(.*?)\n```)?",  # optional JSON args
        re.DOTALL,
    )

    parts: list[ChatMessageEntry] = []
    last_end = 0

    for match in pattern.finditer(content):
        # Text before this tool call
        text_before = content[last_end : match.start()].strip()
        if text_before:
            parts.append({"role": "assistant", "content": text_before})

        tool_name = match.group(1)
        args_str = match.group(2)
        tool_args = None
        if args_str:
            try:
                import json

                tool_args = json.loads(args_str.strip())
            except Exception:
                tool_args = {"raw": args_str.strip()}

        parts.append(
            {
                "role": "tool_call",
                "content": "",
                "toolName": tool_name,
                "toolArgs": tool_args,
                "toolStatus": "done",
                "toolResult": "",
            }
        )
        last_end = match.end()

    # Trailing text after last tool
    trailing = content[last_end:].strip()
    if trailing:
        parts.append({"role": "assistant", "content": trailing})

    # If no matches found, return the whole content as-is
    if not parts:
        parts.append({"role": "assistant", "content": content})

    return parts
