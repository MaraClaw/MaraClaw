from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.config import get_settings
from app.dao.personal_bridge_fleet import admit
from app.dao.personal_burst_lifecycle import cancel_open_for_agent
from app.dao.whatsapp_personal_runtime_dao import runtime_for, startable_since
from app.db.session import connection_ctx
from app.records.whatsapp_personal import PersonalConnection


async def get_personal(agent_id: UUID, *, lock: bool = False) -> PersonalConnection | None:
    async with connection_ctx() as db:
        row = await db.fetchone(
            "SELECT * FROM whatsapp_personal_connections WHERE agent_id=%(id)s" + (" FOR UPDATE" if lock else ""),
            {"id": agent_id},
        )
        return PersonalConnection.from_row(row) if row is not None else None


async def candidates() -> list[PersonalConnection]:
    """Unclaimed desired connections past their retry gate, longest waiting first, from one joined read."""
    async with connection_ctx() as db:
        rows = await db.fetchall(
            "SELECT c.*,cc.extra_config FROM whatsapp_personal_connections c "
            "LEFT JOIN channel_configs cc ON cc.id=c.config_id WHERE c.desired AND c.claim_token IS NULL "
            "ORDER BY c.created_at,c.config_id"
        )
    now = datetime.now(UTC)
    waiting = [(since, row) for row in rows if (since := startable_since(row, now)) is not None]
    waiting.sort(key=lambda entry: entry[0])
    return [PersonalConnection.from_row(row) for _, row in waiting]


async def claim(config_id: UUID) -> PersonalConnection | None:
    """Lease one connection, or ``None`` when it is declined. Raises ``FleetFullError`` at the fleet ceiling."""
    async with connection_ctx() as db:
        # Lock the connection without waiting, so a row held elsewhere is declined and never queues the poll loop, then
        # re-check the durable retry gate, so a concurrent interruption is never bypassed.
        locked = await db.fetchval(
            "SELECT TRUE FROM whatsapp_personal_connections WHERE config_id=%(id)s FOR UPDATE SKIP LOCKED",
            {"id": config_id},
        )
        if not locked:
            return None
        runtime = await runtime_for(config_id, lock=True)
        if runtime.retry_at is not None and runtime.retry_at > datetime.now(UTC):
            return None
        await admit(db, "whatsapp", get_settings().WHATSAPP_PERSONAL_MAX_RUNNING_CHILDREN)
        row = await db.fetchone(
            "UPDATE whatsapp_personal_connections SET claim_token=%(token)s,lease_until=now()+interval '30 seconds' "
            "WHERE config_id=%(id)s AND desired AND claim_token IS NULL "
            "AND state IN ('pending','paused','reconnecting') RETURNING *",
            {"id": config_id, "token": uuid4()},
        )
        return PersonalConnection.from_row(row) if row is not None else None


async def renew(connection: PersonalConnection) -> bool:
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "UPDATE whatsapp_personal_connections SET lease_until=now()+interval '30 seconds' "
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
            "UPDATE channel_configs SET is_connected=FALSE WHERE agent_id=%(id)s AND channel_type='whatsapp'",
            {"id": agent_id},
        )
        await db.execute(
            "UPDATE whatsapp_personal_connections SET desired=FALSE,generation=%(generation)s, "
            "purge_requested=purge_requested OR %(purge)s,qr_value=NULL,qr_expires_at=NULL,revision=revision+1, "
            "state=CASE WHEN claim_token IS NOT NULL THEN 'disconnecting' WHEN %(purge)s THEN 'disconnected' "
            "ELSE 'paused' END,account_id=CASE WHEN claim_token IS NULL AND %(purge)s THEN NULL ELSE account_id END, "
            "pn_jid=CASE WHEN claim_token IS NULL AND %(purge)s THEN NULL ELSE pn_jid END, "
            "lid_jid=CASE WHEN claim_token IS NULL AND %(purge)s THEN NULL ELSE lid_jid END WHERE agent_id=%(id)s",
            {"id": agent_id, "generation": uuid4(), "purge": purge},
        )
        await db.execute(
            "UPDATE whatsapp_personal_sends SET state='cancelled' WHERE state='pending' AND config_id IN "
            "(SELECT config_id FROM whatsapp_personal_connections WHERE agent_id=%(id)s)",
            {"id": agent_id},
        )
        _ = await cancel_open_for_agent(agent_id, "disconnected")
        if purge:
            await db.execute(
                "DELETE FROM whatsapp_personal_auth WHERE config_id IN "
                "(SELECT config_id FROM whatsapp_personal_connections WHERE agent_id=%(id)s)",
                {"id": agent_id},
            )


async def reaped(connection: PersonalConnection) -> bool:
    async with connection_ctx() as db:
        changed = await db.fetchval(
            "UPDATE whatsapp_personal_connections SET claim_token=NULL,lease_until=NULL, "
            "account_id=CASE WHEN purge_requested THEN NULL ELSE account_id END, "
            "pn_jid=CASE WHEN purge_requested THEN NULL ELSE pn_jid END, "
            "lid_jid=CASE WHEN purge_requested THEN NULL ELSE lid_jid END, "
            "state=CASE WHEN desired AND purge_requested THEN 'pending' WHEN desired THEN 'reconnecting' "
            "WHEN purge_requested THEN 'disconnected' ELSE 'paused' END, "
            "auth_epoch=CASE WHEN desired AND purge_requested THEN %(epoch)s ELSE auth_epoch END, "
            "attempt_id=CASE WHEN desired AND purge_requested THEN %(attempt)s ELSE attempt_id END, "
            "activated_at=CASE WHEN purge_requested THEN NULL ELSE activated_at END, "
            "expires_at=CASE WHEN desired AND purge_requested THEN now()+interval '120 seconds' ELSE expires_at END, "
            "purge_requested=FALSE,revision=revision+1,updated_at=now() "
            "WHERE config_id=%(id)s AND claim_token=%(claim)s RETURNING TRUE",
            {"id": connection.config_id, "claim": connection.claim_token, "epoch": uuid4(), "attempt": uuid4()},
        )
        if changed:
            await db.execute(
                "UPDATE channel_configs SET is_connected=FALSE WHERE id=%(id)s", {"id": connection.config_id}
            )
            await db.execute(
                "UPDATE whatsapp_personal_sends SET state='uncertain' WHERE config_id=%(id)s "
                "AND claim_token=%(claim)s AND state='dispatch_started'",
                {"id": connection.config_id, "claim": connection.claim_token},
            )
        return bool(changed)
