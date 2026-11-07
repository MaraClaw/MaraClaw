"""Chat session management API endpoints."""

import base64
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.core.json_types import str_from_row
from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.dao import agent_dao
from app.dao.chat_dao import SessionPosition, chat_message_dao, chat_session_dao
from app.dao.gateway_report_delivery_dao import gateway_report_delivery_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord
from app.services.chat_history import (
    ChatHistoryRefresh,
    ChatMessagePage,
    HeldHistoryQuery,
    get_history_page,
    get_history_refresh,
    parse_cursor,
    parse_held,
)
from app.services.chat_message_projection import ChatMessageEntry, project_messages
from app.services.chat_session_access import can_access_chat_session, can_view_all_agent_chat_sessions
from app.services.shared_chat import ensure_shared_session, is_shared_session, shared_session_id

router = APIRouter(prefix="/api/agents", tags=["chat-sessions"])


class SessionOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    user_id: str
    username: str | None = None
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


class SessionPageOut(BaseModel):
    items: list[SessionOut]
    next_cursor: str | None
    has_more: bool


SessionScope = Literal["mine", "all"]


class SessionCursor(BaseModel):
    """Opaque position of the last row of a page, bound to the agent and scope that issued it."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1] = 1
    agent_id: uuid.UUID
    scope: SessionScope
    last_message_at: AwareDatetime | None
    created_at: AwareDatetime
    session_id: uuid.UUID


def _session_cursor(agent_id: uuid.UUID, scope: SessionScope, last: ChatSessionRecord) -> str:
    if last.created_at is None:
        raise ValueError("Persisted chat session has no creation timestamp")
    cursor = SessionCursor(
        agent_id=agent_id,
        scope=scope,
        last_message_at=last.last_message_at,
        created_at=last.created_at,
        session_id=last.id,
    )
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")


def _parse_session_cursor(raw: str | None, agent_id: uuid.UUID, scope: SessionScope) -> SessionPosition | None:
    if raw is None:
        return None
    try:
        decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        cursor = SessionCursor.model_validate_json(decoded)
    except ValueError:  # binascii.Error, bad UTF-8 and pydantic ValidationError are all ValueErrors
        raise HTTPException(400, "Invalid session cursor") from None
    if cursor.agent_id != agent_id or cursor.scope != scope:
        raise HTTPException(400, "Session cursor belongs to another agent or scope")
    return SessionPosition(cursor.last_message_at, cursor.created_at, cursor.session_id)


class CreateSessionIn(BaseModel):
    title: str | None = None


class PatchSessionIn(BaseModel):
    title: str


async def _all_scope_outs(
    sessions: Sequence[ChatSessionRecord], current_user: UserRecord, *, drop_empty: bool
) -> list[SessionOut]:
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
                "SELECT u.id, COALESCE(NULLIF(u.display_name, ''), NULLIF(i.email, ''), 'User') AS display "
                "FROM users u JOIN identities i ON u.identity_id = i.id "
                "WHERE u.id = ANY(%(ids)s)",
                {"ids": user_ids},
            )
        for row in rows:
            user_names[str(row["id"])] = str_from_row(row.get("display"), "Unknown") or "Unknown"

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
            agent_names[str(row["id"])] = str_from_row(row.get("name"), "Agent") or "Agent"

    for session in sessions:
        count = message_counts.get(str(session.id), 0)
        if drop_empty and count == 0 and not is_shared_session(session):
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
                unread_count=0 if is_shared_session(session) else unread_counts.get(str(session.id), 0),
                is_primary=bool(session.is_primary),
                peer_agent_id=peer_agent_id,
                peer_agent_name=peer_agent_name,
                participant_type="group" if session.is_group else participant_type,
                is_group=session.is_group,
                group_name=session.group_name,
            )
        )
    return out


async def _mine_scope_outs(
    sessions: Sequence[ChatSessionRecord], current_user: UserRecord, agent_id: uuid.UUID, *, drop_empty: bool
) -> list[SessionOut]:
    out: list[SessionOut] = []
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
        if drop_empty and count == 0 and not is_shared_session(session):
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
                unread_count=0 if is_shared_session(session) else unread_counts.get(str(session.id), 0),
                is_primary=bool(session.is_primary),
            )
        )
    return out


async def _session_outs(
    sessions: Sequence[ChatSessionRecord],
    scope: SessionScope,
    current_user: UserRecord,
    agent_id: uuid.UUID,
    *,
    drop_empty: bool,
) -> list[SessionOut]:
    if scope == "all":
        return await _all_scope_outs(sessions, current_user, drop_empty=drop_empty)
    return await _mine_scope_outs(sessions, current_user, agent_id, drop_empty=drop_empty)


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
        if not can_view_all_agent_chat_sessions(current_user, agent):
            raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

        shared = await ensure_shared_session(agent_id)
        sessions = [shared, *(s for s in await chat_session_dao.list_all_for_agent(agent_id) if s.id != shared.id)]
        return await _session_outs(sessions, "all", current_user, agent_id, drop_empty=True)

    # List only the current user's sessions for the remaining scope.
    shared = await ensure_shared_session(agent_id)
    legacy = await chat_session_dao.list_for_user(agent_id=agent_id, user_id=current_user.id)
    sessions = [shared, *(s for s in legacy if s.id != shared.id)]
    return await _session_outs(sessions, "mine", current_user, agent_id, drop_empty=True)


@router.get("/{agent_id}/sessions/page")
async def list_sessions_page(
    agent_id: uuid.UUID,
    scope: SessionScope = Query("mine", description="'mine' or 'all'"),
    limit: int = Query(50, ge=1, le=200, description="Sessions after the shared one; the first page adds the shared"),
    cursor: str | None = Query(None, max_length=1024),
    current_user: UserRecord = Depends(get_current_user),
) -> SessionPageOut:
    """Keyset page of the same rows as the list route; the shared session leads the first page only."""
    agent = await agent_dao.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    _ = await check_agent_access(current_user, agent_id)
    if scope == "all" and not can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized to view all sessions")
    after = _parse_session_cursor(cursor, agent_id, scope)

    rows = list(
        await chat_session_dao.list_page(
            agent_id=agent_id,
            user_id=current_user.id,
            scope=scope,
            exclude_id=shared_session_id(agent_id),
            limit=limit + 1,
            after=after,
        )
    )
    selected = rows[:limit]
    sessions = selected if after is not None else [await ensure_shared_session(agent_id), *selected]
    has_more = len(rows) > limit
    return SessionPageOut(
        items=await _session_outs(sessions, scope, current_user, agent_id, drop_empty=False),
        next_cursor=_session_cursor(agent_id, scope, selected[-1]) if has_more else None,
        has_more=has_more,
    )


@router.get("/{agent_id}/sessions/{session_id}")
async def get_session(
    agent_id: uuid.UUID, session_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
) -> SessionOut:
    """One session the caller may read, in the list-row shape that matches their scope."""
    agent, session = await _authorized_session(agent_id, session_id, current_user)
    scope: SessionScope = "all" if can_view_all_agent_chat_sessions(current_user, agent) else "mine"
    (out,) = await _session_outs([session], scope, current_user, agent_id, drop_empty=False)
    return out


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID, body: CreateSessionIn = CreateSessionIn(), current_user: UserRecord = Depends(get_current_user)
) -> SessionOut:
    """Resume the canonical shared human conversation."""
    _ = body
    _ = await check_agent_access(current_user, agent_id)
    session = await ensure_shared_session(agent_id)
    counts = await chat_session_dao.message_counts([str(session.id)], agent_id=agent_id)
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id),
        user_id=str(session.user_id),
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat() if session.created_at else "",
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=counts.get(str(session.id), 0),
        unread_count=0,
        is_primary=True,
        participant_type="user",
        is_group=False,
    )


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchSessionIn,
    current_user: UserRecord = Depends(get_current_user),
):
    """Rename a session. Owner, agent creator, or admin may rename others' sessions."""
    agent, _ = await check_agent_access(current_user, agent_id)
    session = await chat_session_dao.get_for_agent(session_id, agent_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if str(session.user_id) != str(current_user.id) and not can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized")

    session = await chat_session_dao.update(db_obj=session, obj_in={"title": body.title})
    return {"id": str(session.id), "title": session.title}


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(
    agent_id: uuid.UUID, session_id: uuid.UUID, current_user: UserRecord = Depends(get_current_user)
):
    """Delete a chat session and its messages. Owner, agent creator, or admin may delete others' sessions."""
    agent, _ = await check_agent_access(current_user, agent_id)
    async with connection_ctx():
        session = await chat_session_dao.lock_for_agent(session_id, agent_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if is_shared_session(session):
            raise HTTPException(409, {"reason": "shared_session_required", "session_id": str(session.id)})

        if str(session.user_id) != str(current_user.id) and not can_view_all_agent_chat_sessions(current_user, agent):
            raise HTTPException(status_code=403, detail="Not authorized")

        if await gateway_report_delivery_dao.has_outstanding_session(session_id):
            raise HTTPException(409, {"reason": "session_delivery_pending", "session_id": str(session_id)})

        await chat_message_dao.delete_for_conversation(str(session_id))
        _ = await chat_session_dao.delete(record_id=session_id)


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=500, description="Number of messages to return"),
    before: str | None = Query(None, description="Cursor: return messages created before this timestamp (ISO format)"),
    current_user: UserRecord = Depends(get_current_user),
) -> list[ChatMessageEntry]:
    """Get chat messages for a specific session."""
    session = await _authorize_history(agent_id, session_id, current_user)

    before_dt = None
    if before not in (None, ""):
        try:
            before_dt = datetime.fromisoformat(before)
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
        and session.source_channel not in ("agent", "trigger", "shared")
    ):
        _ = await chat_session_dao.update(
            db_obj=session,
            obj_in={"last_read_at_by_user": datetime.now(UTC)},
        )
    return await project_messages(messages, session.source_channel)


async def _authorized_session(
    agent_id: uuid.UUID, session_id: uuid.UUID, current_user: UserRecord
) -> tuple[AgentRecord, ChatSessionRecord]:
    agent, _ = await check_agent_access(current_user, agent_id)
    session = await chat_session_dao.get_for_agent_or_peer(session_id, agent_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not can_access_chat_session(current_user, agent, session):
        raise HTTPException(status_code=403, detail="Not authorized to view this session")

    return agent, session


async def _authorize_history(agent_id: uuid.UUID, session_id: uuid.UUID, current_user: UserRecord) -> ChatSessionRecord:
    _, session = await _authorized_session(agent_id, session_id, current_user)
    return session


@router.get("/{agent_id}/sessions/{session_id}/messages/page")
async def get_session_messages_page(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    held: Annotated[HeldHistoryQuery, Query()],
    limit: int = Query(50, ge=1, le=500, description="Maximum database rows; inline tool entries stay together"),
    cursor: str | None = Query(None, max_length=1024),
    current_user: UserRecord = Depends(get_current_user),
) -> ChatMessagePage | ChatHistoryRefresh:
    session = await _authorize_history(agent_id, session_id, current_user)
    boundary = parse_cursor(cursor, agent_id, session_id)
    refresh_from = parse_held(held)
    if boundary is not None and refresh_from is not None:
        raise HTTPException(400, "A history cursor cannot be combined with a refresh boundary")
    read_at = datetime.now(UTC)
    page = (
        await get_history_page(session, limit, boundary, agent_id=agent_id)
        if refresh_from is None
        else await get_history_refresh(session, limit, refresh_from, agent_id=agent_id)
    )

    if (
        boundary is None
        and str(session.user_id) == str(current_user.id)
        and not session.is_group
        and session.source_channel not in ("agent", "trigger", "shared")
    ):
        _ = await chat_session_dao.update(
            db_obj=session,
            obj_in={"last_read_at_by_user": read_at},
        )

    return page
