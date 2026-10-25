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
from app.core.security import check_token_generation, load_user_principal
from app.dao.chat_dao import chat_session_dao
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.chat_session_access import can_access_chat_session
from app.services.chat_session_service import ensure_primary_platform_session


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


def ticket_key(ticket: str) -> str:
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    return f"{get_settings().REDIS_KEY_PREFIX}:chat-ticket:{digest}"


async def authorize(binding: ChatTicketBinding) -> tuple[UserRecord, AgentRecord, str]:
    if time.time() >= binding.auth_deadline:
        raise HTTPException(401, "Authentication expired")
    user = await load_user_principal(binding.user_id, fresh=True)
    if user.identity is None:
        raise HTTPException(401, "Identity unavailable")
    await check_token_generation(user.identity.id, binding.auth_generation)
    if user.tenant_id != binding.tenant_id:
        raise HTTPException(403, "Membership changed")
    agent, level = await check_agent_access(user, binding.agent_id, fresh=True)
    if is_agent_expired(agent):
        raise HTTPException(403, "Agent expired")
    return user, agent, level


async def authorized_session(binding: ChatTicketBinding, user: UserRecord, agent: AgentRecord) -> None:
    session = await chat_session_dao.get_for_agent(binding.session_id, binding.agent_id)
    if session is None:
        raise HTTPException(403, "Session unavailable")
    if not can_access_chat_session(user, agent, session):
        raise HTTPException(403, "Not authorized for this session")


async def issue(binding: ChatTicketBinding) -> ChatTicketResponse:
    user, agent, _ = await authorize(binding)
    await authorized_session(binding, user, agent)
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
    request: ChatTicketRequest, user: UserRecord, auth_deadline: int, auth_generation: int
) -> ChatTicketResponse:
    if user.tenant_id is None:
        raise HTTPException(403, "Tenant membership required")
    agent, _ = await check_agent_access(user, request.agent_id, fresh=True)
    if is_agent_expired(agent):
        raise HTTPException(403, "Agent expired")
    binding = ChatTicketBinding(
        user_id=user.id,
        tenant_id=user.tenant_id,
        agent_id=request.agent_id,
        session_id=await resolve_session(request, user.id),
        auth_deadline=auth_deadline,
        auth_generation=auth_generation,
    )
    return await issue(binding)


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
