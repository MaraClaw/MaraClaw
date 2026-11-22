"""Durable retry budgets and sanitized observations under the connection's claim fence."""

from datetime import UTC, datetime, timedelta
from typing import Final, Literal, assert_never
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, JsonValue, ValidationError

from app.core.logging import logger
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection, ZaloDiagnosticError, ZaloError, ZaloRuntime

RETRY_DELAYS: Final = (5, 15, 30, 60, 120)


class _ExtraConfig(BaseModel):
    extra_config: JsonValue = None


async def runtime_for(config_id: UUID, *, lock: bool = False) -> ZaloRuntime:
    async with connection_ctx() as db:
        raw = await db.fetchone(
            "SELECT extra_config FROM channel_configs WHERE id=%(id)s" + (" FOR UPDATE" if lock else ""),
            {"id": config_id},
        )
        if raw is None:
            return ZaloRuntime()
        extra = _ExtraConfig.model_validate(raw).extra_config
        match extra:
            case dict():
                try:
                    return ZaloRuntime.model_validate(extra.get("zalo_runtime", {}))
                except ValidationError:
                    return ZaloRuntime()
            case _:
                return ZaloRuntime()


async def _save(config_id: UUID, runtime: ZaloRuntime) -> None:
    """Caller holds owner, connection and config locks, in that order."""
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE channel_configs SET extra_config=((CASE WHEN jsonb_typeof(extra_config::jsonb)='object' "
            "THEN extra_config::jsonb ELSE '{}'::jsonb END) || "
            "jsonb_build_object('mode','personal_qr','zalo_runtime',%(runtime)s::jsonb))::json,updated_at=now() "
            "WHERE id=%(id)s",
            {"id": config_id, "runtime": runtime.model_dump_json()},
        )
        await db.execute(
            "UPDATE zalo_personal_connections SET revision=revision+1,updated_at=now() WHERE config_id=%(id)s",
            {"id": config_id},
        )


async def reset_runtime(config_id: UUID) -> None:
    """The creator start path already owns the connection and owner locks."""
    await runtime_for(config_id, lock=True)
    await _save(config_id, ZaloRuntime())


async def has_auth(connection: ZaloConnection) -> bool:
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "SELECT TRUE FROM zalo_personal_auth WHERE config_id=%(id)s AND auth_epoch=%(epoch)s",
                {"id": connection.config_id, "epoch": connection.auth_epoch},
            )
        )


async def record_error(connection: ZaloConnection, code: ZaloError) -> None:
    from app.dao.zalo_personal_dao import fenced

    async with connection_ctx():
        current = await fenced(connection)
        runtime = await runtime_for(current.config_id, lock=True)
        await _save(
            current.config_id,
            runtime.model_copy(update={"last_error": code, "last_error_at": datetime.now(UTC), "retry_at": None}),
        )


async def interrupted(connection: ZaloConnection, code: ZaloDiagnosticError = "transport_failed") -> bool:
    """Settle an interruption once; retain the claim until physical reaping."""
    from app.dao.zalo_personal_dao import get_personal, same_claim
    from app.services.channels.zalo_personal_access import require_running, zalo_owner

    async with connection_ctx() as db:
        try:
            agent, owner = await zalo_owner(connection.agent_id, authorization_only=True)
            require_running(agent)
        except HTTPException:
            return False
        current = await get_personal(connection.agent_id, lock=True)
        if (
            current is None
            or not same_claim(current, connection)
            or not current.desired
            or current.purge_requested
            or current.owner_id != owner.id
            or current.tenant_id != agent.tenant_id
        ):
            return False
        runtime = await runtime_for(current.config_id, lock=True)
        if current.state == "reconnecting" and current.expires_at is None and runtime.retry_at is not None:
            return False
        retry = (
            code != "session_replaced"
            and current.account_id is not None
            and await has_auth(current)
            and runtime.reconnect_attempts < len(RETRY_DELAYS)
        )
        now = datetime.now(UTC)
        retry_at = now + timedelta(seconds=RETRY_DELAYS[runtime.reconnect_attempts]) if retry else None
        await _save(
            current.config_id,
            runtime.model_copy(
                update={
                    "reconnect_attempts": runtime.reconnect_attempts + int(retry),
                    "retry_at": retry_at,
                    "last_error": code,
                    "last_error_at": now,
                }
            ),
        )
        await db.execute(
            "UPDATE zalo_personal_connections SET desired=%(retry)s,state=%(state)s,error_code=%(code)s,"
            "expires_at=NULL,qr_value=NULL,qr_expires_at=NULL,revision=revision+1,updated_at=now() WHERE config_id=%(id)s",
            {
                "id": current.config_id,
                "retry": retry,
                "state": "reconnecting" if retry else "disconnecting",
                "code": None if code == "session_replaced" else code,
            },
        )
        await db.execute("UPDATE channel_configs SET is_connected=FALSE WHERE id=%(id)s", {"id": current.config_id})
        logger.bind(config_id=str(current.config_id), code=code, retry=retry).warning("Zalo personal interrupted")
        return True


async def observe(
    connection: ZaloConnection, event: Literal["ready", "heartbeat", "activity"], *, ok: bool = True
) -> None:
    from app.dao.zalo_personal_dao import fenced

    async with connection_ctx():
        current = await fenced(connection, connected=True)
        runtime = await runtime_for(current.config_id, lock=True)
        now = datetime.now(UTC)
        match event:
            case "ready":
                updated = runtime.model_copy(update={"connected_at": now, "retry_at": None})
            case "heartbeat":
                updated = runtime.model_copy(
                    update={"heartbeat_at": now, "heartbeat_failures": 0, "reconnect_attempts": 0, "retry_at": None}
                    if ok
                    else {"heartbeat_failures": min(runtime.heartbeat_failures + 1, 1000)}
                )
                if not ok:
                    logger.bind(config_id=str(current.config_id), code="transport_failed").warning(
                        "Zalo personal heartbeat failed"
                    )
            case "activity":
                if runtime.last_activity_at is not None and (now - runtime.last_activity_at).total_seconds() < 30:
                    return
                updated = runtime.model_copy(update={"last_activity_at": now})
            case _:
                assert_never(event)
        await _save(current.config_id, updated)
