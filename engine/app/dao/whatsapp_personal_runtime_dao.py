"""Durable transient-retry budget for personal WhatsApp, kept credential-free in channel extra_config."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, JsonValue, ValidationError

from app.db.session import connection_ctx
from app.records.whatsapp_personal import PersonalConnection, PersonalRuntime

RETRY_DELAYS: Final = (5, 15, 30, 60, 120)
SUSTAINED_SECONDS: Final = 120.0


class _ExtraConfig(BaseModel):
    extra_config: JsonValue = None


class _Waiter(_ExtraConfig):
    updated_at: AwareDatetime


def _runtime_in(extra: JsonValue) -> PersonalRuntime:
    match extra:
        case dict():
            try:
                return PersonalRuntime.model_validate(extra.get("whatsapp_runtime", {}))
            except ValidationError:
                return PersonalRuntime()
        case _:
            return PersonalRuntime()


async def runtime_for(config_id: UUID, *, lock: bool = False) -> PersonalRuntime:
    async with connection_ctx() as db:
        raw = await db.fetchone(
            "SELECT extra_config FROM channel_configs WHERE id=%(id)s" + (" FOR UPDATE" if lock else ""),
            {"id": config_id},
        )
        return PersonalRuntime() if raw is None else _runtime_in(_ExtraConfig.model_validate(raw).extra_config)


def startable_since(row: Mapping[str, object], now: datetime) -> datetime | None:
    """When a connection row joined with its channel config became startable: its last release, or its retry gate
    if that opened later. ``None`` while the retry gate is still closed at ``now``."""
    waiter = _Waiter.model_validate(row)
    retry_at = _runtime_in(waiter.extra_config).retry_at
    if retry_at is None:
        return waiter.updated_at
    return None if retry_at > now else max(waiter.updated_at, retry_at)


async def _save(config_id: UUID, runtime: PersonalRuntime) -> None:
    """Caller holds the owner, connection and config locks, in that order. Merges only the runtime key."""
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE channel_configs SET extra_config=((CASE WHEN jsonb_typeof(extra_config::jsonb)='object' "
            "THEN extra_config::jsonb ELSE '{}'::jsonb END) || "
            "jsonb_build_object('whatsapp_runtime',%(runtime)s::jsonb))::json,updated_at=now() "
            "WHERE id=%(id)s",
            {"id": config_id, "runtime": runtime.model_dump_json()},
        )


async def reset_runtime(config_id: UUID) -> None:
    """The owner start path already holds the owner and connection locks."""
    await runtime_for(config_id, lock=True)
    await _save(config_id, PersonalRuntime())


async def schedule_retry(config_id: UUID) -> datetime | None:
    """Charge one transient retry; ``None`` once the budget is spent. Caller holds owner and connection locks."""
    runtime = await runtime_for(config_id, lock=True)
    now = datetime.now(UTC)
    spent = runtime.reconnect_attempts >= len(RETRY_DELAYS)
    retry_at = None if spent else now + timedelta(seconds=RETRY_DELAYS[runtime.reconnect_attempts])
    await _save(
        config_id,
        runtime.model_copy(
            update={
                "reconnect_attempts": runtime.reconnect_attempts + int(not spent),
                "retry_at": retry_at,
                "last_error": "transport_failed",
                "last_error_at": now,
            }
        ),
    )
    return retry_at


async def settle_runtime(config_id: UUID) -> None:
    """Refill the budget after sustained uptime. Caller's renewed lease holds the connection row."""
    runtime = await runtime_for(config_id, lock=True)
    if runtime.reconnect_attempts or runtime.retry_at is not None:
        await _save(config_id, runtime.model_copy(update={"reconnect_attempts": 0, "retry_at": None}))


async def has_auth(connection: PersonalConnection) -> bool:
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "SELECT TRUE FROM whatsapp_personal_auth WHERE config_id=%(id)s AND auth_epoch=%(epoch)s "
                "AND category='creds' AND key_id='main'",
                {"id": connection.config_id, "epoch": connection.auth_epoch},
            )
        )
