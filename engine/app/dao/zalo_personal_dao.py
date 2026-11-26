from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection, ZaloError
from app.services.channels.zalo_personal_access import require_running, zalo_owner


async def get_personal(agent_id: UUID, *, lock: bool = False) -> ZaloConnection | None:
    async with connection_ctx() as db:
        row = await db.fetchone(
            "SELECT * FROM zalo_personal_connections WHERE agent_id=%(id)s" + (" FOR UPDATE" if lock else ""),
            {"id": agent_id},
        )
        return ZaloConnection.from_row(row) if row is not None else None


async def candidates() -> list[ZaloConnection]:
    """Unclaimed desired connections past their retry gate, longest waiting first, from one joined read."""
    from app.dao.zalo_personal_runtime_dao import startable_since

    async with connection_ctx() as db:
        rows = await db.fetchall(
            "SELECT c.*,cc.extra_config FROM zalo_personal_connections c "
            "LEFT JOIN channel_configs cc ON cc.id=c.config_id WHERE c.desired AND c.claim_token IS NULL "
            "AND c.state IN ('pending','reconnecting') ORDER BY c.created_at,c.config_id"
        )
    now = datetime.now(UTC)
    waiting = [(since, row) for row in rows if (since := startable_since(row, now)) is not None]
    waiting.sort(key=lambda entry: entry[0])
    return [ZaloConnection.from_row(row) for _, row in waiting]


async def claim(config_id: UUID) -> ZaloConnection | None:
    from app.dao.zalo_personal_runtime_dao import runtime_for

    async with connection_ctx() as db:
        source = await db.fetchone("SELECT * FROM zalo_personal_connections WHERE config_id=%(id)s", {"id": config_id})
        if source is None:
            return None
        candidate = ZaloConnection.from_row(source)
        try:
            agent, owner = await zalo_owner(candidate.agent_id, authorization_only=True)
            require_running(agent)
        except HTTPException:
            return None
        current = await get_personal(candidate.agent_id, lock=True)
        if (
            current is None
            or current.config_id != config_id
            or current.owner_id != owner.id
            or current.tenant_id != agent.tenant_id
            or not current.desired
            or current.claim_token is not None
            or current.state not in {"pending", "reconnecting"}
        ):
            return None
        runtime = await runtime_for(config_id, lock=True)
        if runtime.retry_at is not None and runtime.retry_at > datetime.now(UTC):
            return None
        row = await db.fetchone(
            "UPDATE zalo_personal_connections SET claim_token=%(claim)s,"
            "state=CASE WHEN account_id IS NULL THEN 'pending' ELSE 'reconnecting' END,"
            "lease_until=now()+interval '30 seconds',expires_at=now()+interval '120 seconds',"
            "updated_at=now() WHERE config_id=%(id)s AND desired AND claim_token IS NULL "
            "AND state IN ('pending','reconnecting') RETURNING *",
            {"id": config_id, "claim": uuid4()},
        )
        return ZaloConnection.from_row(row) if row is not None else None


def same_claim(current: ZaloConnection, owned: ZaloConnection) -> bool:
    return (
        current.config_id == owned.config_id
        and current.auth_epoch == owned.auth_epoch
        and current.generation == owned.generation
        and current.claim_token == owned.claim_token
        and owned.claim_token is not None
        and current.owner_id == owned.owner_id
        and current.tenant_id == owned.tenant_id
    )


async def fenced(connection: ZaloConnection, *, connected: bool = False) -> ZaloConnection:
    agent, owner = await zalo_owner(connection.agent_id, authorization_only=True)
    require_running(agent)
    current = await get_personal(agent.id, lock=True)
    if (
        current is None
        or not same_claim(current, connection)
        or not current.desired
        or current.owner_id != owner.id
        or current.tenant_id != agent.tenant_id
        or current.lease_until is None
        or current.lease_until <= datetime.now(UTC)
        or (connected and (current.state != "connected" or current.account_id is None))
    ):
        raise HTTPException(409, "Zalo source revoked")
    return current


async def renew(connection: ZaloConnection) -> bool:
    async with connection_ctx() as db:
        await fenced(connection)
        return bool(
            await db.fetchval(
                "UPDATE zalo_personal_connections SET lease_until=now()+interval '30 seconds',updated_at=now() "
                "WHERE config_id=%(id)s AND claim_token=%(claim)s AND generation=%(generation)s "
                "AND auth_epoch=%(epoch)s AND desired AND lease_until>now() RETURNING TRUE",
                {
                    "id": connection.config_id,
                    "claim": connection.claim_token,
                    "generation": connection.generation,
                    "epoch": connection.auth_epoch,
                },
            )
        )


async def revoke(agent_id: UUID, *, purge: bool) -> None:
    async with connection_ctx() as db:
        await db.execute(
            "UPDATE channel_configs SET is_connected=FALSE WHERE agent_id=%(id)s AND channel_type='zalo'",
            {"id": agent_id},
        )
        await db.execute(
            "UPDATE zalo_personal_connections SET desired=FALSE,generation=%(generation)s,"
            "purge_requested=purge_requested OR %(purge)s,qr_value=NULL,qr_expires_at=NULL,revision=revision+1,"
            "updated_at=now(),state=CASE WHEN claim_token IS NOT NULL THEN 'disconnecting' "
            "WHEN %(purge)s THEN 'disconnected' ELSE 'paused' END,"
            "account_id=CASE WHEN claim_token IS NULL AND %(purge)s THEN NULL ELSE account_id END WHERE agent_id=%(id)s",
            {"id": agent_id, "generation": uuid4(), "purge": purge},
        )
        await db.execute(
            "UPDATE zalo_personal_sends SET state='cancelled',finished_at=now() WHERE state='pending' "
            "AND config_id IN (SELECT config_id FROM zalo_personal_connections WHERE agent_id=%(id)s)",
            {"id": agent_id},
        )
        await db.execute(
            "UPDATE zalo_personal_media_sends SET state='cancelled',finished_at=now() WHERE state='pending' "
            "AND config_id IN (SELECT config_id FROM zalo_personal_connections WHERE agent_id=%(id)s)",
            {"id": agent_id},
        )
        if purge:
            await db.execute(
                "DELETE FROM zalo_personal_auth WHERE config_id IN "
                "(SELECT config_id FROM zalo_personal_connections WHERE agent_id=%(id)s)",
                {"id": agent_id},
            )


async def reaped(connection: ZaloConnection) -> bool:
    """Only call after wait() proves the exact owned child exited. Lease expiry is not proof."""
    async with connection_ctx() as db:
        changed = await db.fetchval(
            "UPDATE zalo_personal_connections SET claim_token=NULL,lease_until=NULL,qr_value=NULL,qr_expires_at=NULL,"
            "account_id=CASE WHEN purge_requested THEN NULL ELSE account_id END,"
            "state=CASE WHEN desired AND purge_requested THEN 'pending' WHEN desired THEN 'reconnecting' "
            "WHEN error_code IN ('qr_expired') THEN 'expired' WHEN error_code IS NOT NULL THEN 'failed' "
            "WHEN purge_requested THEN 'disconnected' ELSE 'paused' END,"
            "auth_epoch=CASE WHEN desired AND purge_requested THEN %(epoch)s ELSE auth_epoch END,"
            "attempt_id=CASE WHEN desired AND purge_requested THEN %(attempt)s ELSE attempt_id END,"
            "activated_at=NULL,expires_at=NULL,"
            "purge_requested=FALSE,revision=revision+1,updated_at=now() "
            "WHERE config_id=%(id)s AND claim_token=%(claim)s RETURNING TRUE",
            {"id": connection.config_id, "claim": connection.claim_token, "epoch": uuid4(), "attempt": uuid4()},
        )
        if changed:
            await db.execute(
                "UPDATE channel_configs SET is_connected=FALSE WHERE id=%(id)s", {"id": connection.config_id}
            )
            await db.execute(
                "UPDATE zalo_personal_sends SET state='uncertain',finished_at=now() WHERE config_id=%(id)s "
                "AND claim_token=%(claim)s AND state='dispatch_started'",
                {"id": connection.config_id, "claim": connection.claim_token},
            )
            await db.execute(
                "UPDATE zalo_personal_media_sends SET state='uncertain',finished_at=now() WHERE config_id=%(id)s "
                "AND claim_token=%(claim)s AND state='dispatch_started'",
                {"id": connection.config_id, "claim": connection.claim_token},
            )
        return bool(changed)


async def fail(connection: ZaloConnection, code: ZaloError) -> None:
    from app.dao.zalo_personal_runtime_dao import record_error

    async with connection_ctx() as db:
        try:
            agent, owner = await zalo_owner(connection.agent_id, authorization_only=True)
            require_running(agent)
        except HTTPException:
            return
        current = await get_personal(connection.agent_id, lock=True)
        if (
            current is None
            or not same_claim(current, connection)
            or not current.desired
            or current.owner_id != owner.id
            or current.tenant_id != agent.tenant_id
            or (current.state == "reconnecting" and current.expires_at is None)
        ):
            return
        await record_error(current, code)
        await revoke(connection.agent_id, purge=code in {"login_failed", "qr_expired", "qr_declined"})
        await db.execute(
            "UPDATE zalo_personal_connections SET error_code=%(code)s WHERE config_id=%(id)s",
            {"id": connection.config_id, "code": code},
        )
