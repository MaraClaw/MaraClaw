"""Redis share-token store for public file links: TTL-only grants, never the raw token at rest."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter, ValidationError
from redis.exceptions import RedisError

from app.config import get_settings
from app.core.events import get_redis

ISSUE_ATTEMPTS = 3


class FileShareStoreUnavailableError(Exception):
    """The share-token store is unreachable or would not store a new token; the route layer maps it to 503."""


@dataclass(frozen=True, slots=True)
class FileShareGrant:
    agent_id: uuid.UUID
    path: str


_grant_json = TypeAdapter(FileShareGrant)


def token_key(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"{get_settings().REDIS_KEY_PREFIX}:file-share:{digest}"


async def issue(agent_id: uuid.UUID, path: str, ttl_seconds: int) -> tuple[str, datetime]:
    """Store a grant under a fresh token that stays valid until its TTL runs out, however often it is resolved."""
    payload = _grant_json.dump_json(FileShareGrant(agent_id=agent_id, path=path)).decode()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    try:
        redis = await get_redis()
        for _ in range(ISSUE_ATTEMPTS):
            token = secrets.token_urlsafe(32)
            if bool(await redis.set(token_key(token), payload, nx=True, ex=ttl_seconds)):
                return token, expires_at
    except RedisError as exc:
        raise FileShareStoreUnavailableError("File share store unavailable") from exc
    raise FileShareStoreUnavailableError("File share store would not accept a new token")


async def resolve(token: str) -> FileShareGrant | None:
    """The grant behind a live token, or None for an unknown, expired or unreadable one."""
    try:
        redis = await get_redis()
        value = await redis.get(token_key(token))
    except RedisError as exc:
        raise FileShareStoreUnavailableError("File share store unavailable") from exc
    if value is None:
        return None
    try:
        return _grant_json.validate_json(value)
    except ValidationError:
        return None
