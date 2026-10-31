import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException

from app.config import get_settings
from app.core.domain.channels import ChannelType
from app.core.permissions import is_agent_expired
from app.dao.channel_config_dao import channel_config_dao
from app.dao.whatsapp_personal_dao import get_personal, revoke
from app.db.session import connection_ctx
from app.records.whatsapp_personal import PersonalConnection
from app.schemas.whatsapp_personal import PersonalQR, PersonalStatus
from app.services.channels.whatsapp_personal_access import personal_owner
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey


def public_status(connection: PersonalConnection | None) -> PersonalStatus:
    if connection is None:
        return PersonalStatus()
    now = datetime.now(UTC)
    qr = None
    if (
        connection.qr_value is not None
        and connection.qr_expires_at is not None
        and connection.qr_expires_at > now
        and connection.desired
    ):
        value = AuthCipher(get_settings().SECRET_KEY).open(
            AuthKey(connection.config_id, connection.auth_epoch, "qr", str(connection.attempt_id)),
            base64.b64decode(connection.qr_value, validate=True),
        )
        qr = PersonalQR(value=value, expires_at=connection.qr_expires_at)
    return PersonalStatus(
        generation=connection.generation,
        attempt_id=connection.attempt_id,
        revision=connection.revision,
        state=connection.state,
        expires_at=connection.expires_at,
        qr=qr,
        account_hint=("***" + connection.pn_jid.split("@", 1)[0][-4:]) if connection.pn_jid is not None else None,
        error_code=connection.error_code,
    )


async def status(agent_id: UUID, actor_id: UUID) -> PersonalStatus:
    async with connection_ctx():
        await personal_owner(agent_id, actor_id)
        connection = await get_personal(agent_id, lock=True)
        if (
            connection
            and connection.state in {"pending", "qr"}
            and connection.expires_at
            and connection.expires_at <= datetime.now(UTC)
        ):
            await revoke(agent_id, purge=True)
            async with connection_ctx() as db:
                await db.execute(
                    "UPDATE whatsapp_personal_connections SET state=CASE WHEN claim_token IS NULL "
                    "THEN 'expired' ELSE 'disconnecting' END WHERE agent_id=%(id)s",
                    {"id": agent_id},
                )
            connection = await get_personal(agent_id)
        return public_status(connection)


async def start(agent_id: UUID, actor_id: UUID, expected: UUID | None, *, relink: bool = False) -> PersonalStatus:
    async with connection_ctx() as db:
        agent, owner = await personal_owner(agent_id, actor_id)
        if is_agent_expired(agent) or agent.status == "stopped":
            raise HTTPException(404, "Agent unavailable")
        connection = await get_personal(agent_id, lock=True)
        if (connection.generation if connection else None) != expected:
            raise HTTPException(409, "Stale WhatsApp generation")
        if connection and connection.claim_token is not None:
            if relink:
                await revoke(agent_id, purge=True)
                await db.execute(
                    "UPDATE whatsapp_personal_connections SET desired=TRUE WHERE agent_id=%(id)s", {"id": agent_id}
                )
                return public_status(await get_personal(agent_id))
            raise HTTPException(409, "Connector teardown pending")
        config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type=ChannelType.WHATSAPP)
        if config is None:
            config = await channel_config_dao.create(obj_in={"agent_id": agent_id, "channel_type": "whatsapp"})
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
        resumable = connection is not None and not relink and not connection.purge_requested
        epoch = connection.auth_epoch if connection and resumable else uuid4()
        if not resumable:
            await db.execute("DELETE FROM whatsapp_personal_auth WHERE config_id=%(id)s", {"id": config.id})
        await db.execute(
            "INSERT INTO whatsapp_personal_connections(config_id,agent_id,owner_id,tenant_id,auth_epoch,generation, "
            "attempt_id,expires_at) VALUES(%(id)s,%(agent)s,%(owner)s,%(tenant)s,%(epoch)s,%(generation)s, "
            "%(attempt)s,%(expires)s) ON CONFLICT(agent_id) DO UPDATE SET config_id=EXCLUDED.config_id, "
            "owner_id=EXCLUDED.owner_id,tenant_id=EXCLUDED.tenant_id,auth_epoch=EXCLUDED.auth_epoch, "
            "generation=EXCLUDED.generation,attempt_id=EXCLUDED.attempt_id,expires_at=EXCLUDED.expires_at, "
            "state='pending',desired=TRUE,purge_requested=FALSE,qr_value=NULL,qr_expires_at=NULL,error_code=NULL, "
            "activated_at=NULL,revision=whatsapp_personal_connections.revision+1, "
            "account_id=CASE WHEN %(resume)s THEN whatsapp_personal_connections.account_id ELSE NULL END, "
            "pn_jid=CASE WHEN %(resume)s THEN whatsapp_personal_connections.pn_jid ELSE NULL END, "
            "lid_jid=CASE WHEN %(resume)s THEN whatsapp_personal_connections.lid_jid ELSE NULL END",
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


async def disconnect(agent_id: UUID, actor_id: UUID, expected: UUID) -> PersonalStatus:
    async with connection_ctx():
        await personal_owner(agent_id, actor_id)
        connection = await get_personal(agent_id, lock=True)
        if connection is None or connection.generation != expected:
            raise HTTPException(409, "Stale WhatsApp generation")
        await revoke(agent_id, purge=True)
        return public_status(await get_personal(agent_id))
