"""Shared, single-use roster credentials; never store a CSV or session JWT."""

import hashlib
import secrets
import time
from typing import ClassVar, Final
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.domain.users import UserRole
from app.core.events import get_redis
from app.core.security import check_token_generation, load_user_principal
from app.dao.tenant_dao import tenant_dao
from app.records.user import UserRecord

TICKET_TTL_SECONDS: Final = 60


class UserRosterExportOut(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)
    ticket: str
    expires_in: int


class RosterTicketBinding(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)
    user_id: UUID
    identity_id: UUID
    actor_tenant_id: UUID | None
    tenant_id: UUID
    search: str
    auth_deadline: int
    auth_generation: int = Field(strict=True, ge=0)


def company_roster_tenant(tenant_id: str | None, user: UserRecord) -> UUID:
    """Resolve company scope without confusing actor membership with PA target."""
    if user.role not in (UserRole.PLATFORM_ADMIN, UserRole.ORG_ADMIN):
        raise HTTPException(403, "Admin access required")
    if user.role == UserRole.ORG_ADMIN:
        if user.tenant_id is None:
            raise HTTPException(403, "Organization admin must belong to a company")
        return user.tenant_id
    if tenant_id is None or tenant_id == "":
        raise HTTPException(400, "tenant_id is required")
    try:
        return UUID(tenant_id)
    except ValueError as exc:
        raise HTTPException(400, "Invalid tenant_id") from exc


def ticket_key(ticket: str) -> str:
    return f"{get_settings().REDIS_KEY_PREFIX}:roster-ticket:{hashlib.sha256(ticket.encode()).hexdigest()}"


async def issue(binding: RosterTicketBinding) -> UserRosterExportOut:
    ticket = secrets.token_urlsafe(32)
    try:
        redis = await get_redis()
        stored = await redis.set(ticket_key(ticket), binding.model_dump_json(), nx=True, ex=TICKET_TTL_SECONDS)
    except RedisError as exc:
        raise HTTPException(503, "Download authentication unavailable") from exc
    if not bool(stored):
        raise HTTPException(503, "Download authentication unavailable")
    return UserRosterExportOut(ticket=ticket, expires_in=TICKET_TTL_SECONDS)


async def redeem(ticket: str) -> RosterTicketBinding:
    try:
        redis = await get_redis()
        value = await redis.getdel(ticket_key(ticket))
    except RedisError as exc:
        raise HTTPException(503, "Download authentication unavailable") from exc
    if value is None:
        raise HTTPException(400, "Invalid download ticket")
    try:
        return RosterTicketBinding.model_validate_json(value)
    except ValidationError as exc:
        raise HTTPException(400, "Invalid download ticket") from exc


async def authorize(binding: RosterTicketBinding) -> UserRecord:
    if time.time() >= binding.auth_deadline:
        raise HTTPException(401, "Authentication expired")
    user = await load_user_principal(binding.user_id, fresh=True)
    if user.identity is None or user.identity.id != binding.identity_id:
        raise HTTPException(401, "Identity unavailable")
    await check_token_generation(binding.identity_id, binding.auth_generation)
    if user.tenant_id != binding.actor_tenant_id:
        raise HTTPException(403, "Membership changed")
    if company_roster_tenant(str(binding.tenant_id), user) != binding.tenant_id:
        raise HTTPException(403, "Company scope changed")
    if await tenant_dao.get(binding.tenant_id) is None:
        raise HTTPException(404, "Company not found")
    return user
