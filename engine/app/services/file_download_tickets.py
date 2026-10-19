"""Single-use, shared Redis credentials for browser file downloads."""

import hashlib
import secrets
import time
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.events import get_redis
from app.core.permissions import check_agent_access
from app.core.security import load_user_principal
from app.records.user import UserRecord

TICKET_TTL_SECONDS = 60


class DownloadTicketRequest(BaseModel):
    path: str
    inline: bool = False


class DownloadTicketResponse(BaseModel):
    ticket: str
    expires_in: int


class DownloadTicketBinding(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    path: str
    inline: bool
    auth_deadline: int


def normalize_download_path(path: str) -> str:
    return path.strip().strip("/")


def ticket_key(ticket: str) -> str:
    digest = hashlib.sha256(ticket.encode()).hexdigest()
    return f"{get_settings().REDIS_KEY_PREFIX}:file-ticket:{digest}"


async def issue(binding: DownloadTicketBinding) -> DownloadTicketResponse:
    ticket = secrets.token_urlsafe(32)
    try:
        redis = await get_redis()
        stored = await redis.set(ticket_key(ticket), binding.model_dump_json(), nx=True, ex=TICKET_TTL_SECONDS)
    except RedisError as exc:
        raise HTTPException(503, "Download authentication unavailable") from exc
    if not bool(stored):
        raise HTTPException(503, "Download authentication unavailable")
    return DownloadTicketResponse(ticket=ticket, expires_in=TICKET_TTL_SECONDS)


async def create(
    user: UserRecord, agent_id: uuid.UUID, path: str, inline: bool, auth_deadline: int
) -> DownloadTicketResponse:
    if user.tenant_id is None:
        raise HTTPException(403, "Tenant membership required")
    binding = DownloadTicketBinding(
        user_id=user.id,
        tenant_id=user.tenant_id,
        agent_id=agent_id,
        path=normalize_download_path(path),
        inline=inline,
        auth_deadline=auth_deadline,
    )
    return await issue(binding)


async def redeem(ticket: str) -> DownloadTicketBinding:
    try:
        redis = await get_redis()
        value = await redis.getdel(ticket_key(ticket))
    except RedisError as exc:
        raise HTTPException(503, "Download authentication unavailable") from exc
    if value is None:
        raise HTTPException(400, "Invalid download ticket")
    try:
        return DownloadTicketBinding.model_validate_json(value)
    except ValidationError as exc:
        raise HTTPException(400, "Invalid download ticket") from exc


async def authorize_binding(
    binding: DownloadTicketBinding, *, agent_id: uuid.UUID, path: str, inline: bool
) -> UserRecord:
    """Re-check the bound principal and request; the caller still maps visibility with the returned tenant."""
    if time.time() >= binding.auth_deadline:
        raise HTTPException(401, "Authentication expired")
    if (binding.agent_id, binding.path, binding.inline) != (agent_id, normalize_download_path(path), inline):
        raise HTTPException(403, "Ticket does not match this download")
    user = await load_user_principal(binding.user_id, fresh=True)
    if user.identity is None:
        raise HTTPException(401, "Identity unavailable")
    if user.tenant_id != binding.tenant_id:
        raise HTTPException(403, "Membership changed")
    _ = await check_agent_access(user, agent_id, fresh=True)
    return user
