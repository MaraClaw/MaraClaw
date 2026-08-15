"""Add, remove, and pick Linkup API keys from the database ring."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.dao.linkup_api_key_dao import linkup_api_key_dao
from app.db.errors import UniqueViolationError
from app.records.linkup_api_key import LinkupApiKeyRecord

EXHAUSTED_COOLDOWN = timedelta(hours=24)


class DuplicateLinkupKeyError(Exception):
    """The same API secret is already stored."""


class LinkupKeyNotFoundError(Exception):
    """No key row for the given id."""


def fingerprint_api_key(api_key: str) -> str:
    """Stable hash used for uniqueness and admin display."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def decrypt_api_key(record: LinkupApiKeyRecord) -> str:
    return decrypt_data(record.key_ciphertext, get_settings().SECRET_KEY)


def _now() -> datetime:
    return datetime.now(UTC)


def public_key_view(record: LinkupApiKeyRecord) -> dict[str, object]:
    """Admin-safe dict: no ciphertext."""
    return {
        "id": record.id,
        "label": record.label,
        "fingerprint": record.key_fingerprint,
        "position": record.position,
        "status": record.status,
        "exhausted_until": record.exhausted_until,
        "last_used_at": record.last_used_at,
        "created_at": record.created_at,
    }


async def list_keys() -> list[LinkupApiKeyRecord]:
    return list(await linkup_api_key_dao.list_ordered())


async def add_key(*, label: str, api_key: str) -> LinkupApiKeyRecord:
    """Append an active key. Sets the cursor if the ring was empty."""
    secret = api_key.strip()
    if not secret:
        raise ValueError("api_key is required")
    clean_label = label.strip() or "Linkup key"
    fingerprint = fingerprint_api_key(secret)
    existing = await linkup_api_key_dao.get_by_fingerprint(fingerprint)
    if existing is not None:
        raise DuplicateLinkupKeyError("This Linkup API key is already stored")

    max_position = await linkup_api_key_dao.max_position()
    position = 0 if max_position is None else max_position + 1
    ciphertext = encrypt_data(secret, get_settings().SECRET_KEY)
    try:
        record = await linkup_api_key_dao.create(
            obj_in={
                "label": clean_label,
                "key_ciphertext": ciphertext,
                "key_fingerprint": fingerprint,
                "position": position,
                "status": "active",
            }
        )
    except UniqueViolationError as exc:
        raise DuplicateLinkupKeyError("This Linkup API key is already stored") from exc

    cursor = await linkup_api_key_dao.get_cursor_key_id()
    if cursor is None:
        await linkup_api_key_dao.set_cursor_key_id(record.id)
    return record


async def remove_key(key_id: UUID) -> LinkupApiKeyRecord:
    """Hard-delete a key and retarget the cursor if it pointed here."""
    existing = await linkup_api_key_dao.get(key_id)
    if existing is None:
        raise LinkupKeyNotFoundError(str(key_id))

    cursor = await linkup_api_key_dao.get_cursor_key_id()
    deleted = await linkup_api_key_dao.delete(id=key_id)
    if deleted is None:
        raise LinkupKeyNotFoundError(str(key_id))

    if cursor == key_id:
        nxt = await _next_after(existing, now=_now())
        await linkup_api_key_dao.set_cursor_key_id(nxt.id if nxt is not None else None)
    return deleted


async def _next_after(removed: LinkupApiKeyRecord, *, now: datetime) -> LinkupApiKeyRecord | None:
    active = list(await linkup_api_key_dao.list_active_ordered(now=now))
    if not active:
        return None
    for record in active:
        if record.position > removed.position:
            return record
    return active[0]


async def mark_exhausted(key_id: UUID, *, message: str, until: datetime | None = None) -> None:
    record = await linkup_api_key_dao.get(key_id)
    if record is None:
        return
    cooldown = until or (_now() + EXHAUSTED_COOLDOWN)
    _ = await linkup_api_key_dao.update(
        db_obj=record,
        obj_in={"exhausted_until": cooldown, "last_error": message[:500]},
    )


async def touch_used(key_id: UUID) -> None:
    record = await linkup_api_key_dao.get(key_id)
    if record is None:
        return
    _ = await linkup_api_key_dao.update(db_obj=record, obj_in={"last_used_at": _now()})


async def advance_cursor(from_key_id: UUID) -> LinkupApiKeyRecord | None:
    """Move the cursor to the next active key after ``from_key_id``."""
    current = await linkup_api_key_dao.get(from_key_id)
    nxt = await _next_after(
        current
        if current is not None
        else LinkupApiKeyRecord(
            id=from_key_id,
            label="",
            key_ciphertext="",
            key_fingerprint="",
            position=-1,
        ),
        now=_now(),
    )
    await linkup_api_key_dao.set_cursor_key_id(nxt.id if nxt is not None else None)
    return nxt


async def current_key() -> LinkupApiKeyRecord | None:
    """Return the cursor key if it is still usable, else the first active key."""
    now = _now()
    active = list(await linkup_api_key_dao.list_active_ordered(now=now))
    if not active:
        return None
    cursor_id = await linkup_api_key_dao.get_cursor_key_id()
    if cursor_id is not None:
        for record in active:
            if record.id == cursor_id:
                return record
    first = active[0]
    await linkup_api_key_dao.set_cursor_key_id(first.id)
    return first


async def has_active_keys() -> bool:
    return bool(await linkup_api_key_dao.list_active_ordered(now=_now()))


async def ensure_env_key_seeded() -> None:
    """Insert env LINKUP_API_KEY as the first row when the table is empty."""
    if not await linkup_api_key_dao.is_empty():
        return
    env_key = get_settings().LINKUP_API_KEY.strip()
    if not env_key:
        return
    try:
        _ = await add_key(label="Environment LINKUP_API_KEY", api_key=env_key)
    except DuplicateLinkupKeyError:
        return


async def should_use_linkup_proxy() -> bool:
    settings = get_settings()
    if not settings.LINKUP_PROXY_ENABLED:
        return False
    if not settings.LINKUP_PROXY_BASE_URL.strip():
        return False
    await ensure_env_key_seeded()
    return await has_active_keys() or bool(settings.LINKUP_API_KEY.strip())
