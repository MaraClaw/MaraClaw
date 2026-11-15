from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from fastapi import HTTPException

from app.config import get_settings
from app.core.domain.channels import ChannelType
from app.dao.channel_config_dao import channel_config_dao
from app.dao.zalo_personal_auth_dao import PURPOSE
from app.dao.zalo_personal_dao import get_personal, revoke
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection
from app.schemas.zalo_personal import ZaloPersonalQR, ZaloPersonalStatus
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey
from app.services.channels.zalo_personal_access import require_running, zalo_owner


def public_status(connection: ZaloConnection | None) -> ZaloPersonalStatus:
    if connection is None:
        return ZaloPersonalStatus()
    qr = None
    if (
        connection.state == "qr"
        and connection.desired
        and connection.qr_value is not None
        and connection.qr_expires_at is not None
        and connection.qr_expires_at > datetime.now(UTC)
    ):
        value = AuthCipher(get_settings().SECRET_KEY, purpose=PURPOSE).open(
            AuthKey(connection.config_id, connection.auth_epoch, "qr", str(connection.attempt_id)),
            connection.qr_value,
        )
        qr = ZaloPersonalQR(value=value, expires_at=connection.qr_expires_at)
    return ZaloPersonalStatus(
        generation=connection.generation,
        attempt_id=connection.attempt_id,
        revision=connection.revision,
        state=connection.state,
        expires_at=connection.expires_at,
        qr=qr,
        account_hint="***" + connection.account_id[-4:] if connection.account_id is not None else None,
        error_code=connection.error_code,
    )


async def status(agent_id: UUID, actor_id: UUID) -> ZaloPersonalStatus:
    async with connection_ctx():
        await zalo_owner(agent_id, actor_id, authorization_only=True)
        row = await get_personal(agent_id)
        expired = (
            row is not None
            and row.state in {"pending", "qr", "reconnecting"}
            and row.expires_at is not None
            and row.expires_at <= datetime.now(UTC)
        )
        if not expired:
            try:
                return public_status(row)
            except InvalidTag, UnicodeError:
                pass
    async with connection_ctx() as db:
        await zalo_owner(agent_id, actor_id)
        row = await get_personal(agent_id, lock=True)
        if row is not None:
            expired = (
                row.state in {"pending", "qr", "reconnecting"}
                and row.expires_at is not None
                and row.expires_at <= datetime.now(UTC)
            )
            if expired:
                await revoke(agent_id, purge=True)
                await db.execute(
                    "UPDATE zalo_personal_connections SET error_code='qr_expired',"
                    "state=CASE WHEN claim_token IS NULL THEN 'expired' ELSE 'disconnecting' END "
                    "WHERE agent_id=%(id)s",
                    {"id": agent_id},
                )
            else:
                try:
                    return public_status(row)
                except InvalidTag, UnicodeError:
                    await revoke(agent_id, purge=True)
                    await db.execute(
                        "UPDATE zalo_personal_connections SET error_code='auth_store_failed',"
                        "state=CASE WHEN claim_token IS NULL THEN 'failed' ELSE 'disconnecting' END "
                        "WHERE agent_id=%(id)s",
                        {"id": agent_id},
                    )
        return public_status(await get_personal(agent_id))


async def start(agent_id: UUID, actor_id: UUID, expected: UUID | None, *, relink: bool = False) -> ZaloPersonalStatus:
    async with connection_ctx() as db:
        agent, owner = await zalo_owner(agent_id, actor_id)
        require_running(agent)
        row = await get_personal(agent_id, lock=True)
        if (row.generation if row else None) != expected:
            raise HTTPException(409, "Stale Zalo generation")
        if row is not None and row.claim_token is not None:
            if not relink:
                raise HTTPException(409, "Connector teardown pending")
            await revoke(agent_id, purge=True)
            await db.execute(
                "UPDATE zalo_personal_connections SET desired=TRUE,error_code=NULL WHERE agent_id=%(id)s",
                {"id": agent_id},
            )
            return public_status(await get_personal(agent_id))
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.ZALO)
        if config is None:
            config = await channel_config_dao.create(obj_in={"agent_id": agent_id, "channel_type": "zalo"})
        if row is not None and row.config_id != config.id:
            await db.execute(
                "DELETE FROM zalo_personal_connections WHERE config_id=%(id)s AND claim_token IS NULL",
                {"id": row.config_id},
            )
            row = None
        await channel_config_dao.update(
            db_obj=config,
            obj_in={
                "app_id": None,
                "app_secret": None,
                "encrypt_key": None,
                "verification_token": None,
                "extra_config": {"mode": "personal_qr"},
                "is_configured": True,
                "is_connected": False,
            },
        )
        resumable = (
            row is not None
            and not relink
            and not row.purge_requested
            and row.owner_id == owner.id
            and row.tenant_id == agent.tenant_id
        )
        epoch = row.auth_epoch if row is not None and resumable else uuid4()
        if not resumable:
            await db.execute("DELETE FROM zalo_personal_auth WHERE config_id=%(id)s", {"id": config.id})
        await db.execute(
            "INSERT INTO zalo_personal_connections(config_id,agent_id,owner_id,tenant_id,auth_epoch,generation,"
            "attempt_id,expires_at) VALUES(%(id)s,%(agent)s,%(owner)s,%(tenant)s,%(epoch)s,%(generation)s,"
            "%(attempt)s,%(expires)s) ON CONFLICT(agent_id) DO UPDATE SET owner_id=EXCLUDED.owner_id,"
            "tenant_id=EXCLUDED.tenant_id,auth_epoch=EXCLUDED.auth_epoch,generation=EXCLUDED.generation,"
            "attempt_id=EXCLUDED.attempt_id,expires_at=EXCLUDED.expires_at,state='pending',desired=TRUE,"
            "purge_requested=FALSE,qr_value=NULL,qr_expires_at=NULL,error_code=NULL,activated_at=NULL,"
            "revision=zalo_personal_connections.revision+1,updated_at=now(),"
            "account_id=CASE WHEN %(resume)s THEN zalo_personal_connections.account_id ELSE NULL END",
            {
                "id": config.id,
                "agent": agent_id,
                "owner": owner.id,
                "tenant": agent.tenant_id,
                "epoch": epoch,
                "generation": uuid4(),
                "attempt": uuid4(),
                "expires": datetime.now(UTC) + timedelta(seconds=120),
                "resume": resumable,
            },
        )
        return public_status(await get_personal(agent_id))


async def disconnect(agent_id: UUID, actor_id: UUID, expected: UUID) -> ZaloPersonalStatus:
    async with connection_ctx():
        await zalo_owner(agent_id, actor_id)
        row = await get_personal(agent_id, lock=True)
        if row is None or row.generation != expected:
            raise HTTPException(409, "Stale Zalo generation")
        await revoke(agent_id, purge=True)
        return public_status(await get_personal(agent_id))
