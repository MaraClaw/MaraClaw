"""Shared Take Control ownership and canonical AgentBay session identity."""

import hashlib
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError

from app.core.events import get_redis

LEASE_TTL_SECONDS = 120
REGISTRY_TTL_SECONDS = 3600


class TakeControlUnavailableError(RuntimeError):
    pass


class TakeControlLostError(RuntimeError):
    pass


class SessionUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionKey:
    agent_id: str
    chat_id: str
    image_type: str

    @property
    def lease_key(self) -> str:
        digest = hashlib.sha256(f"{self.agent_id}:{self.chat_id}".encode()).hexdigest()
        return f"take-control:{{{digest}}}:lease"

    @property
    def registry_key(self) -> str:
        return f"{self.lease_key}:session:{self.image_type}"


class RemoteSession(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    remote_id: str
    image: str
    last_used: float = 0


class Lease(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    user_id: str
    owner_token: str
    image_type: str
    remote_id: str


_ACQUIRE = """
local current = redis.call('GET', KEYS[1])
if current then return current end
local remote = redis.call('GET', KEYS[2])
if not remote then return '' end
local lease = cjson.decode(ARGV[1])
lease.remote_id = cjson.decode(remote).remote_id
local encoded = cjson.encode(lease)
redis.call('SET', KEYS[1], encoded, 'EX', ARGV[2])
redis.call('EXPIRE', KEYS[2], ARGV[3])
return encoded
"""
_RENEW = """
local current = redis.call('GET', KEYS[1])
if not current then return '' end
local lease = cjson.decode(current)
if lease.user_id ~= ARGV[1] or lease.owner_token ~= ARGV[2] then return '' end
local registry = KEYS[1] .. ':session:' .. lease.image_type
local remote = redis.call('GET', registry)
if not remote or cjson.decode(remote).remote_id ~= lease.remote_id then return '' end
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('EXPIRE', registry, ARGV[4])
return current
"""
_RELEASE = """
local current = redis.call('GET', KEYS[1])
if not current then return 0 end
local lease = cjson.decode(current)
if lease.user_id ~= ARGV[1] or lease.owner_token ~= ARGV[2] then return 0 end
return redis.call('DEL', KEYS[1])
"""
_REGISTER = """
local current = redis.call('GET', KEYS[1])
if current then return current end
local remote = cjson.decode(ARGV[1])
remote.last_used = tonumber(redis.call('TIME')[1])
local encoded = cjson.encode(remote)
redis.call('SET', KEYS[1], encoded, 'EX', ARGV[2])
return encoded
"""
_DROP = """
local current = redis.call('GET', KEYS[1])
if not current or cjson.decode(current).remote_id ~= ARGV[1] then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
if cjson.decode(current).last_used > tonumber(redis.call('TIME')[1]) - 300 then return 0 end
return redis.call('DEL', KEYS[1])
"""
_FORGET = """
local current = redis.call('GET', KEYS[1])
if not current or cjson.decode(current).remote_id ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""
_GET_REMOTE = """
local current = redis.call('GET', KEYS[1])
if not current then return false end
local remote = cjson.decode(current)
remote.last_used = tonumber(redis.call('TIME')[1])
local encoded = cjson.encode(remote)
redis.call('SET', KEYS[1], encoded, 'EX', ARGV[1])
return encoded
"""


class _RedisState(Protocol):
    def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> Awaitable[object]: ...


async def _connection() -> _RedisState:
    return await get_redis()


async def _eval(script: str, keys: list[str], args: list[str | int]) -> object:
    try:
        redis = await _connection()
        return await redis.eval(script, len(keys), *keys, *args)
    except (RedisError, OSError) as error:
        raise TakeControlUnavailableError("Take Control state unavailable") from error


async def get_remote(key: SessionKey) -> RemoteSession | None:
    result = await _eval(_GET_REMOTE, [key.registry_key], [REGISTRY_TTL_SECONDS])
    return RemoteSession.model_validate_json(result) if isinstance(result, str) else None


async def register_remote(key: SessionKey, remote: RemoteSession) -> RemoteSession:
    result = await _eval(_REGISTER, [key.registry_key], [remote.model_dump_json(), REGISTRY_TTL_SECONDS])
    if not isinstance(result, str):
        raise TakeControlUnavailableError("Invalid session registry response")
    return RemoteSession.model_validate_json(result)


async def drop_remote(key: SessionKey, remote_id: str) -> bool:
    return bool(await _eval(_DROP, [key.registry_key, key.lease_key], [remote_id]))


async def forget_released_remote(key: SessionKey, remote_id: str) -> None:
    await _eval(_FORGET, [key.registry_key], [remote_id])


async def acquire(key: SessionKey, user_id: str, owner_token: str | None) -> tuple[Lease, bool]:
    if owner_token not in (None, ""):
        return await renew(key, user_id, owner_token), True
    candidate = Lease(user_id=user_id, owner_token=secrets.token_urlsafe(32), image_type=key.image_type, remote_id="")
    result = await _eval(
        _ACQUIRE,
        [key.lease_key, key.registry_key],
        [candidate.model_dump_json(), LEASE_TTL_SECONDS, REGISTRY_TTL_SECONDS],
    )
    if not isinstance(result, str) or not result:
        raise SessionUnavailableError("No active session for this agent, chat and environment")
    lease = Lease.model_validate_json(result)
    return lease, secrets.compare_digest(lease.owner_token, candidate.owner_token)


async def renew(key: SessionKey, user_id: str, owner_token: str | None) -> Lease:
    result = await _eval(_RENEW, [key.lease_key], [user_id, owner_token or "", LEASE_TTL_SECONDS, REGISTRY_TTL_SECONDS])
    if not isinstance(result, str) or not result:
        raise TakeControlLostError("Take Control lease lost")
    return Lease.model_validate_json(result)


async def release(key: SessionKey, user_id: str, owner_token: str | None) -> None:
    if not await _eval(_RELEASE, [key.lease_key], [user_id, owner_token or ""]):
        raise TakeControlLostError("Take Control lease lost")


async def is_session_locked(agent_id: str, session_id: str) -> bool:
    key = SessionKey(agent_id, session_id, "browser")
    return bool(await _eval("return redis.call('EXISTS', KEYS[1])", [key.lease_key], []))
