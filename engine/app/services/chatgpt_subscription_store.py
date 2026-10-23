"""Redis single-use PKCE tickets and owner-bound, short-lived recovery results."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.events import get_redis
from app.core.security import encrypt_data
from app.records.user import UserRecord
from app.services.chatgpt_pkce import BrowserAuthorization
from app.services.chatgpt_subscription_types import ChatGPTSubscriptionStatusOut

ATTEMPT_TTL = 600
RESULT_TTL = 300


class AttemptBinding(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    actor_id: UUID
    identity_id: UUID
    actor_tenant_id: UUID | None
    tenant_id: UUID
    expires_at: datetime

    def require_owner(self, user: UserRecord) -> None:
        if (user.id, user.identity_id, user.tenant_id) != (self.actor_id, self.identity_id, self.actor_tenant_id):
            raise HTTPException(status_code=404, detail="ChatGPT attempt not found")


class PendingTicket(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    binding: AttemptBinding
    state: str
    verifier_encrypted: str


class StoredResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    binding: AttemptBinding
    result: ChatGPTSubscriptionStatusOut


@dataclass(frozen=True, slots=True)
class AttemptView:
    binding: AttemptBinding
    result: ChatGPTSubscriptionStatusOut | None = None


def _key(attempt_id: str, kind: str) -> str:
    digest = hashlib.sha256(attempt_id.encode()).hexdigest()
    return f"{get_settings().REDIS_KEY_PREFIX}chatgpt-oauth:{digest}:{kind}"


def unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="ChatGPT sign-in storage unavailable")


async def issue(binding: AttemptBinding, authorization: BrowserAuthorization) -> str:
    attempt_id = secrets.token_urlsafe(32)
    ticket = PendingTicket(
        attempt_id=attempt_id,
        binding=binding,
        state=authorization.state,
        verifier_encrypted=encrypt_data(authorization.verifier, get_settings().SECRET_KEY),
    )
    try:
        redis = await get_redis()
        bound = await redis.set(
            _key(attempt_id, "owner"), binding.model_dump_json(), nx=True, ex=ATTEMPT_TTL + RESULT_TTL
        )
        if not bool(bound):
            raise unavailable()
        saved = await redis.set(_key(attempt_id, "pending"), ticket.model_dump_json(), nx=True, ex=ATTEMPT_TTL)
    except RedisError:
        raise unavailable() from None
    if not bool(saved):
        raise unavailable()
    return attempt_id


def new_binding(user: UserRecord, tenant_id: UUID) -> AttemptBinding:
    if user.identity_id is None:
        raise HTTPException(status_code=403, detail="Administrator identity required")
    return AttemptBinding(
        actor_id=user.id,
        identity_id=user.identity_id,
        actor_tenant_id=user.tenant_id,
        tenant_id=tenant_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=ATTEMPT_TTL),
    )


async def read_attempt(attempt_id: str, user: UserRecord) -> AttemptView | None:
    try:
        redis = await get_redis()
        result = await redis.get(_key(attempt_id, "result"))
        if result is not None:
            stored = StoredResult.model_validate_json(result)
            stored.binding.require_owner(user)
            return AttemptView(stored.binding, stored.result)
        owner = await redis.get(_key(attempt_id, "owner"))
        if owner is None:
            return None
        binding = AttemptBinding.model_validate_json(owner)
        binding.require_owner(user)
        return AttemptView(binding)
    except RedisError, ValidationError:
        raise unavailable() from None


async def pending_ticket(attempt_id: str) -> PendingTicket | None:
    try:
        redis = await get_redis()
        raw = await redis.get(_key(attempt_id, "pending"))
        return PendingTicket.model_validate_json(raw) if raw is not None else None
    except RedisError, ValidationError:
        raise unavailable() from None


async def claim(attempt_id: str, binding: AttemptBinding) -> PendingTicket | None:
    try:
        redis = await get_redis()
        raw = await redis.getdel(_key(attempt_id, "pending"))
        if raw is None:
            return None
        ticket = PendingTicket.model_validate_json(raw)
        if ticket.binding != binding:
            raise unavailable()
        return ticket
    except RedisError, ValidationError:
        raise unavailable() from None


async def finish(binding: AttemptBinding, result: ChatGPTSubscriptionStatusOut) -> None:
    try:
        redis = await get_redis()
        saved = await redis.set(
            _key(result.attempt_id, "result"),
            StoredResult(binding=binding, result=result).model_dump_json(),
            nx=True,
            ex=RESULT_TTL,
        )
    except RedisError:
        raise unavailable() from None
    if not bool(saved):
        raise unavailable()
