"""Short-TTL Redis cache for authenticated user+identity snapshots.

Never stores ``password_hash`` or quota *counters*. Redis errors fall back to
SQL. Privilege fields are version-gated: writers INCR after commit.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from app.config import get_settings
from app.core.cache_contracts import SessionCacheObservation, SessionIdentitySnapshot, SessionSnapshot
from app.core.domain.users import UserRole
from app.core.json_types import (
    is_json_object,
    json_as_bool,
    json_as_int,
    json_as_str,
    json_as_str_or,
    json_object_from,
)
from app.core.logging import logger
from app.core.redis_cache import (
    bump_version,
    cache_delete,
    cache_key,
    cache_set_json,
    ensure_version,
    has_pending_snapshot_writes,
    read_session_keys,
    read_version,
)
from app.core.row_memo import memo_drop, memo_drop_kind, memo_get, memo_set
from app.records.identity import IdentityRecord
from app.records.user import UserRecord

_MEMO_KIND = "session_user"
# Which identity each membership last had in this process, so a read can name the identity generation key without
# first reading the snapshot. Only a hint: the snapshot's own identity must match it, otherwise the read is a miss.
_IDENTITY_HINTS_MAX: Final = 10_000
_identity_hints = OrderedDict[UUID, UUID]()


def _ttl() -> int:
    return get_settings().USER_SESSION_CACHE_TTL_SECONDS or 0


def _sess_key(user_id: UUID) -> str:
    return cache_key("sess", "v1", user_id)


def user_version_key(user_id: UUID) -> str:
    return cache_key("sessver", "u", user_id)


def identity_version_key(identity_id: UUID) -> str:
    return cache_key("sessver", "i", identity_id)


def _parse_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _parse_dt(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _snapshot(user: UserRecord, user_ver: str, ident_ver: str) -> SessionSnapshot:
    identity = user.identity
    ident_payload: SessionIdentitySnapshot | None = None
    if identity is not None:
        ident_payload = {
            "id": str(identity.id),
            "email": identity.email,
            "phone": identity.phone,
            "is_active": identity.is_active,
            "is_platform_admin": identity.is_platform_admin,
            "email_verified": identity.email_verified,
            "must_change_password": identity.must_change_password,
            "created_at": identity.created_at.isoformat() if identity.created_at else None,
            "updated_at": identity.updated_at.isoformat() if identity.updated_at else None,
        }
    return {
        "u_ver": user_ver,
        "i_ver": ident_ver,
        "user": {
            "id": str(user.id),
            "identity_id": str(user.identity_id) if user.identity_id else None,
            "tenant_id": str(user.tenant_id) if user.tenant_id else None,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "title": user.title,
            "timezone": user.timezone,
            "role": user.role,
            "is_active": user.is_active,
            "registration_source": user.registration_source,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "updated_at": user.updated_at.isoformat() if user.updated_at else None,
            "quota_message_limit": user.quota_message_limit,
            "quota_message_period": user.quota_message_period,
            "quota_max_agents": user.quota_max_agents,
            "quota_agent_ttl_hours": user.quota_agent_ttl_hours,
            "is_genesis": user.is_genesis,
        },
        "identity": ident_payload,
    }


def _from_snapshot(data: object) -> UserRecord | None:
    payload = json_object_from(data)
    raw_user = payload.get("user")
    if not is_json_object(raw_user):
        return None
    user_id = _parse_uuid(raw_user.get("id"))
    if user_id is None:
        return None
    identity = None
    raw_ident = payload.get("identity")
    if is_json_object(raw_ident) and raw_ident.get("id") is not None:
        ident_id = _parse_uuid(raw_ident.get("id"))
        if ident_id is None:
            return None
        identity = IdentityRecord(
            id=ident_id,
            email=json_as_str(raw_ident.get("email")),
            phone=json_as_str(raw_ident.get("phone")),
            password_hash=None,
            is_active=json_as_bool(raw_ident.get("is_active"), True),
            is_platform_admin=json_as_bool(raw_ident.get("is_platform_admin")),
            email_verified=json_as_bool(raw_ident.get("email_verified")),
            must_change_password=json_as_bool(raw_ident.get("must_change_password")),
            created_at=_parse_dt(raw_ident.get("created_at")),
            updated_at=_parse_dt(raw_ident.get("updated_at")),
        )
    return UserRecord(
        id=user_id,
        identity_id=_parse_uuid(raw_user.get("identity_id")),
        tenant_id=_parse_uuid(raw_user.get("tenant_id")),
        display_name=json_as_str_or(raw_user.get("display_name")),
        avatar_url=json_as_str(raw_user.get("avatar_url")),
        title=json_as_str(raw_user.get("title")),
        timezone=json_as_str(raw_user.get("timezone")),
        role=UserRole(json_as_str_or(raw_user.get("role")) or UserRole.MEMBER),
        is_active=json_as_bool(raw_user.get("is_active"), True),
        registration_source=json_as_str(raw_user.get("registration_source")),
        created_at=_parse_dt(raw_user.get("created_at")),
        updated_at=_parse_dt(raw_user.get("updated_at")),
        quota_message_limit=json_as_int(raw_user.get("quota_message_limit"), 50),
        quota_message_period=json_as_str_or(raw_user.get("quota_message_period"), "permanent") or "permanent",
        quota_messages_used=0,
        quota_period_start=None,
        quota_max_agents=json_as_int(raw_user.get("quota_max_agents"), 2),
        quota_agent_ttl_hours=json_as_int(raw_user.get("quota_agent_ttl_hours")),
        is_genesis=json_as_bool(raw_user.get("is_genesis")),
        identity=identity,
    )


async def peek_user_version(user_id: UUID) -> str | None:
    if _ttl() <= 0 or has_pending_snapshot_writes():
        return None
    return await ensure_version(user_version_key(user_id), ttl=_ttl() * 40)


async def peek_identity_version(identity_id: UUID | None) -> str | None:
    if identity_id is None:
        return "0"
    if _ttl() <= 0 or has_pending_snapshot_writes():
        return None
    return await ensure_version(identity_version_key(identity_id), ttl=_ttl() * 40)


def _remember_identity(user_id: UUID, identity_id: UUID | None) -> None:
    """Record the identity a membership has (forget the hint when it has none) and evict the oldest past the cap."""
    if identity_id is None:
        _ = _identity_hints.pop(user_id, None)
        return
    _identity_hints[user_id] = identity_id
    _identity_hints.move_to_end(user_id)
    while len(_identity_hints) > _IDENTITY_HINTS_MAX:
        _ = _identity_hints.popitem(last=False)


async def _identity_generation(
    user_id: UUID, identity_id: UUID, hint: UUID | None, hinted_generation: str | None
) -> str | None:
    """The live generation of the identity a current snapshot names, or None when it cannot be vouched for.

    Without a hint the generation is read now, after the user generation, in a second round trip. A hint that named
    another identity read the wrong key, so that read is a miss and the hint is corrected for the next one.
    """
    if hint is None:
        read = await read_version(identity_version_key(identity_id))
        return read.value if read is not None else None
    if hint != identity_id:
        _remember_identity(user_id, identity_id)
        return None
    return hinted_generation


async def get_cached_user(user_id: UUID) -> UserRecord | None:
    memo = memo_get(_MEMO_KIND, user_id)
    if isinstance(memo, UserRecord):
        return memo
    if _ttl() <= 0 or has_pending_snapshot_writes():
        return None
    hint = _identity_hints.get(user_id)
    keys = await read_session_keys(
        _sess_key(user_id), user_version_key(user_id), None if hint is None else identity_version_key(hint)
    )
    if keys is None or not is_json_object(keys.snapshot):
        return None
    cached = keys.snapshot
    user = _from_snapshot(cached)
    if user is None or user.id != user_id:
        return None
    if user.identity_id != (user.identity.id if user.identity else None):
        return None
    if keys.user_version is None or cached.get("u_ver") != keys.user_version:
        return None
    if user.identity_id is not None:
        identity_version = await _identity_generation(user_id, user.identity_id, hint, keys.identity_version)
        if identity_version is None or cached.get("i_ver") != identity_version:
            return None
    _remember_identity(user_id, user.identity_id)
    memo_set(_MEMO_KIND, user_id, user)
    logger.debug("session_cache hit")
    return user


async def set_cached_user(
    user: UserRecord,
    *,
    observed: SessionCacheObservation | None = None,
) -> None:
    memo_set(_MEMO_KIND, user.id, user)
    if _ttl() <= 0 or has_pending_snapshot_writes():
        return
    _remember_identity(user.id, user.identity_id)
    if observed is None or user.identity_id != observed.identity_id:
        return
    user_ver = await read_version(user_version_key(user.id))
    if user_ver is None or user_ver.value is None or user_ver.value != observed.user_version:
        return
    if user.identity_id is not None:
        ident_ver = await read_version(identity_version_key(user.identity_id))
        if ident_ver is None or ident_ver.value is None or ident_ver.value != observed.identity_version:
            return
    payload = _snapshot(user, observed.user_version, observed.identity_version)
    _ = await cache_set_json(_sess_key(user.id), payload, ttl=_ttl())


async def bump_user_session(user_id: UUID | None) -> None:
    if user_id is None:
        return
    memo_drop(_MEMO_KIND, user_id)
    if _ttl() <= 0:
        return
    await bump_version(user_version_key(user_id), ttl=_ttl() * 40)
    await cache_delete(_sess_key(user_id), defer_until_commit=True)


async def bump_identity_session(identity_id: UUID | None) -> None:
    if identity_id is None:
        return
    memo_drop_kind(_MEMO_KIND, identity_id=identity_id)
    if _ttl() <= 0:
        return
    await bump_version(identity_version_key(identity_id), ttl=_ttl() * 40)
    members: list[UserRecord] = []
    try:
        from app.dao.user_dao import user_dao

        members = list(await user_dao.get_by_identity_id(identity_id))
    except Exception:
        members = []
    for member in members:
        memo_drop(_MEMO_KIND, member.id)
        await cache_delete(_sess_key(member.id), defer_until_commit=True)


async def bump_user_sessions(user_ids: Sequence[object]) -> None:
    for item in user_ids:
        parsed = item if isinstance(item, UUID) else _parse_uuid(item)
        if parsed is not None:
            await bump_user_session(parsed)
