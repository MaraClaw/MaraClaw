from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.dao.whatsapp_personal_dao import get_personal
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.records.whatsapp_personal import PersonalConnection, PersonalRoute, PersonalSource
from app.services.channels.whatsapp_personal_access import personal_owner
from app.services.channels.whatsapp_personal_protocol import Message


async def resolve_personal_sender(source: PersonalSource, agent_id: UUID) -> tuple[AgentRecord, UserRecord]:
    agent, owner = await personal_owner(agent_id)
    connection = await get_personal(agent_id, lock=True)
    if (
        connection is None
        or not connection.desired
        or connection.state != "connected"
        or connection.config_id != source.config_id
        or connection.generation != source.generation
        or connection.auth_epoch != source.auth_epoch
        or connection.claim_token != source.claim_token
        or connection.account_id != source.account_id
        or connection.owner_id != owner.id
        or connection.tenant_id != agent.tenant_id
        or connection.lease_until is None
        or connection.lease_until <= datetime.now(UTC)
    ):
        raise HTTPException(403, "Personal WhatsApp source revoked")
    async with connection_ctx() as db:
        echo = await db.fetchval(
            "SELECT TRUE FROM whatsapp_personal_sends WHERE account_id=%(account)s AND message_id=%(message)s",
            {"account": source.account_id, "message": source.message_id},
        )
        if echo:
            raise HTTPException(403, "Assistant echo")
    return agent, owner


async def accept_message(connection: PersonalConnection, message: Message) -> bool:
    from app.services.channels.submissions import ChannelEvent, accept_channel_event

    if (
        not message.from_me
        or message.participant is not None
        or message.remote_jid not in {connection.pn_jid, connection.lid_jid}
        or connection.account_id is None
        or connection.claim_token is None
        or connection.activated_at is None
        or message.timestamp < int(connection.activated_at.timestamp())
        or message.timestamp > int(datetime.now(UTC).timestamp()) + 60
    ):
        return False
    source = PersonalSource(
        connection.config_id,
        connection.auth_epoch,
        connection.generation,
        connection.claim_token,
        connection.account_id,
        message.message_id,
    )
    route = PersonalRoute(
        config_id=connection.config_id,
        auth_epoch=connection.auth_epoch,
        generation=connection.generation,
        account_id=connection.account_id,
        self_jid=connection.pn_jid or connection.lid_jid or "",
    )
    await accept_channel_event(
        ChannelEvent(
            config_id=connection.config_id,
            agent_id=connection.agent_id,
            provider="whatsapp",
            event_key=f"personal:{connection.auth_epoch}:{message.message_id}",
            sender_id=connection.account_id,
            conversation_id=route.external_id(),
            text=message.text,
            personal_source=source,
        )
    )
    return True
