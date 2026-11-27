"""Single-use, shared Redis credentials for first-party chat sockets."""

import hashlib
import secrets
import time
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.events import get_redis
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import check_generations, load_user_principal
from app.dao.chat_dao import chat_session_dao
from app.db.session import optional_connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatSessionRecord
from app.records.user import UserRecord
from app.services.chat_session_access import can_access_chat_session
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.shared_chat import is_canonical


class ChatTicketRequest(BaseModel):
    agent_id: uuid.UUID
    session_id: uuid.UUID | None = None


class ChatTicketResponse(BaseModel):
    ticket: str
    session_id: uuid.UUID


class ChatTicketBinding(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    auth_deadline: int
    auth_generation: int = Field(strict=True, ge=0)
    membership_generation: int = Field(default=0, strict=True, ge=0)
    # Set by ``issue`` from the session: a canonical session's socket re-authorizes before each delivery it gets.
    authorize_deliveries: bool = False


def ticket_key(ticket: str) -> str:
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    return f"{get_settings().REDIS_KEY_PREFIX}:chat-ticket:{digest}"


def is_expired(binding: ChatTicketBinding) -> bool:
    """The binding outlived its absolute deadline, the expiry of the access token it was issued from."""
    return time.time() >= binding.auth_deadline


async def authorize(binding: ChatTicketBinding) -> tuple[UserRecord, AgentRecord, str]:
    if is_expired(binding):
        raise HTTPException(401, "Authentication expired")
    return await authorize_identity(binding)


async def authorize_identity(binding: ChatTicketBinding) -> tuple[UserRecord, AgentRecord, str]:
    """Everything ``authorize`` checks except the deadline, which belongs to each socket rather than to its identity.

    Human-transcript delivery runs this once for every socket bound to the same identity and checks each socket's own
    deadline with ``is_expired``.
    """
    user = await load_user_principal(binding.user_id, fresh=True)
    agent, level = await authorize_principal(
        user,
        binding.agent_id,
        tenant_id=binding.tenant_id,
        auth_generation=binding.auth_generation,
        membership_generation=binding.membership_generation,
    )
    return user, agent, level


async def authorize_principal(
    user: UserRecord, agent_id: uuid.UUID, *, tenant_id: uuid.UUID, auth_generation: int, membership_generation: int
) -> tuple[AgentRecord, str]:
    """Authorize a principal the caller has already loaded: both generations, its tenant, then access to the agent."""
    if user.identity is None:
        raise HTTPException(401, "Identity unavailable")
    await check_generations(user.id, user.identity.id, auth=auth_generation, membership=membership_generation)
    if user.tenant_id != tenant_id:
        raise HTTPException(403, "Membership changed")
    agent, level = await check_agent_access(user, agent_id, fresh=True, user_is_current=True)
    if is_agent_expired(agent):
        raise HTTPException(403, "Agent expired")
    return agent, level


async def authorized_session(binding: ChatTicketBinding, user: UserRecord, agent: AgentRecord) -> ChatSessionRecord:
    """Return the binding's session if ``user`` may still open it; 403 otherwise."""
    session = await chat_session_dao.get_for_agent(binding.session_id, binding.agent_id)
    if session is None:
        raise HTTPException(403, "Session unavailable")
    if not can_access_chat_session(user, agent, session):
        raise HTTPException(403, "Not authorized for this session")
    return session


async def authorize_message(binding: ChatTicketBinding) -> tuple[UserRecord, AgentRecord, str]:
    """Authorize one socket message and confirm its session still opens, every read on one pooled checkout."""
    async with optional_connection_ctx():
        user, agent, level = await authorize(binding)
        _ = await authorized_session(binding, user, agent)
    return user, agent, level


async def issue(
    binding: ChatTicketBinding, authorized: tuple[UserRecord, AgentRecord] | None = None
) -> ChatTicketResponse:
    """Store a ticket for ``binding``; ``authorized`` is a user and agent the caller has just authorized for it."""
    async with optional_connection_ctx():
        if authorized is None:
            user, agent, _ = await authorize(binding)
        else:
            user, agent = authorized
        session = await authorized_session(binding, user, agent)
    binding = binding.model_copy(update={"authorize_deliveries": is_canonical(session)})
    ticket = secrets.token_urlsafe(32)
    try:
        redis = await get_redis()
        stored = await redis.set(ticket_key(ticket), binding.model_dump_json(), nx=True, ex=60)
    except RedisError as exc:
        raise HTTPException(503, "Chat authentication unavailable") from exc
    if not bool(stored):
        raise HTTPException(503, "Chat authentication unavailable")
    return ChatTicketResponse(ticket=ticket, session_id=binding.session_id)


async def resolve_session(request: ChatTicketRequest, user_id: uuid.UUID) -> uuid.UUID:
    if request.session_id is not None:
        return request.session_id
    session = await ensure_primary_platform_session(request.agent_id, user_id)
    return session.id


async def create(
    request: ChatTicketRequest, user: UserRecord, auth_deadline: int, auth_generation: int, membership_generation: int
) -> ChatTicketResponse:
    if user.tenant_id is None:
        raise HTTPException(403, "Tenant membership required")
    async with optional_connection_ctx():
        agent, _ = await authorize_principal(
            user,
            request.agent_id,
            tenant_id=user.tenant_id,
            auth_generation=auth_generation,
            membership_generation=membership_generation,
        )
        session_id = await resolve_session(request, user.id)
    binding = ChatTicketBinding(
        user_id=user.id,
        tenant_id=user.tenant_id,
        agent_id=request.agent_id,
        session_id=session_id,
        auth_deadline=auth_deadline,
        auth_generation=auth_generation,
        membership_generation=membership_generation,
    )
    return await issue(binding, (user, agent))


async def redeem(ticket: str) -> ChatTicketBinding:
    try:
        redis = await get_redis()
        value = await redis.getdel(ticket_key(ticket))
    except RedisError as exc:
        raise HTTPException(503, "Chat authentication unavailable") from exc
    if value is None:
        raise HTTPException(400, "Invalid chat ticket")
    try:
        return ChatTicketBinding.model_validate_json(value)
    except ValidationError as exc:
        raise HTTPException(400, "Invalid chat ticket") from exc
