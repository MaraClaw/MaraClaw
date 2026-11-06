"""Server-issued, single-use Redis state for the SSO authorize, callback and bind steps."""

import hashlib
import secrets
from enum import StrEnum
from typing import ClassVar

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.events import get_redis

STATE_TTL_SECONDS = 600
MAX_STATE_LENGTH = 128
MAX_REDIRECT_URI_LENGTH = 2048
INVALID_STATE_DETAIL = "OAuth state is invalid or expired. Please sign in again."


class OAuthIntent(StrEnum):
    SIGN_IN = "sign_in"
    BIND = "bind"


class OAuthStateRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    provider: str
    redirect_uri: str
    intent: OAuthIntent


def unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="OAuth state storage unavailable")


def _key(state: str) -> str:
    digest = hashlib.sha256(state.encode()).hexdigest()
    return f"{get_settings().REDIS_KEY_PREFIX}:oauth-state:{digest}"


async def issue(provider: str, redirect_uri: str, intent: OAuthIntent) -> str:
    state = secrets.token_urlsafe(32)
    record = OAuthStateRecord(provider=provider, redirect_uri=redirect_uri, intent=intent)
    try:
        redis = await get_redis()
        stored = await redis.set(_key(state), record.model_dump_json(), nx=True, ex=STATE_TTL_SECONDS)
    except RedisError as exc:
        raise unavailable() from exc
    if not bool(stored):
        raise unavailable()
    return state


async def consume(state: str, provider: str, intent: OAuthIntent) -> OAuthStateRecord:
    """Atomically read and delete the state, then require its stored provider and intent to match."""
    try:
        redis = await get_redis()
        raw = await redis.getdel(_key(state))
    except RedisError as exc:
        raise unavailable() from exc
    if raw is None:
        raise HTTPException(status_code=400, detail=INVALID_STATE_DETAIL)
    try:
        record = OAuthStateRecord.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=INVALID_STATE_DETAIL) from exc
    if record.provider != provider or record.intent is not intent:
        raise HTTPException(status_code=400, detail=INVALID_STATE_DETAIL)
    return record
