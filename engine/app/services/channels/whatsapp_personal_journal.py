from dataclasses import dataclass
from typing import ClassVar, Literal, override
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from app.dao.whatsapp_personal_dao import get_personal
from app.db.session import connection_ctx
from app.records.outbound_plan import OutboundChunk, OutboundTarget
from app.records.whatsapp_personal import PersonalConnection, PersonalRoute
from app.services.channels.whatsapp_personal_access import personal_owner


@dataclass(slots=True)
class PersonalSendTerminalError(Exception):
    code: str

    @override
    def __str__(self) -> str:
        return self.code


class PersonalSend(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    command_id: UUID
    config_id: UUID
    auth_epoch: UUID
    generation: UUID
    account_id: str
    self_jid: str
    text: str
    message_id: str
    state: Literal["pending", "dispatch_started", "accepted", "rejected", "uncertain", "cancelled"]
    claim_token: UUID | None = None


def route_matches(connection: PersonalConnection, route: PersonalRoute) -> bool:
    return (
        connection.config_id == route.config_id
        and connection.auth_epoch == route.auth_epoch
        and connection.generation == route.generation
        and connection.account_id == route.account_id
        and route.self_jid in {connection.pn_jid, connection.lid_jid}
        and connection.desired
        and connection.state == "connected"
    )


async def enqueue(target: OutboundTarget, chunk: OutboundChunk) -> None:
    try:
        route = PersonalRoute.from_external_id(target.destination)
    except ValueError:
        raise PersonalSendTerminalError("personal_route_invalid") from None
    async with connection_ctx() as db:
        # Route is immutable; a duplicate worker may only observe the original command.
        await db.execute(
            "INSERT INTO whatsapp_personal_sends(command_id,config_id,auth_epoch,generation,account_id,self_jid,"
            "text,message_id) VALUES(%(command)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,%(jid)s,"
            "%(text)s,%(message)s) ON CONFLICT(command_id) DO NOTHING",
            {
                "command": chunk.key,
                "config": route.config_id,
                "epoch": route.auth_epoch,
                "generation": route.generation,
                "account": route.account_id,
                "jid": route.self_jid,
                "text": chunk.text,
                "message": uuid4().hex.upper(),
            },
        )
        raw = await db.fetchone("SELECT * FROM whatsapp_personal_sends WHERE command_id=%(id)s", {"id": chunk.key})
        row = PersonalSend.model_validate(raw)
        if (
            row.config_id != route.config_id
            or row.auth_epoch != route.auth_epoch
            or row.generation != route.generation
            or row.account_id != route.account_id
            or row.self_jid != route.self_jid
            or row.text != chunk.text
        ):
            raise PersonalSendTerminalError("personal_target_changed")
    match row.state:
        case "accepted":
            return
        case "rejected" | "uncertain" | "cancelled":
            raise PersonalSendTerminalError("personal_send_" + row.state)
        case "pending" | "dispatch_started":
            raise PersonalSendPendingError


@dataclass(slots=True)
class PersonalSendPendingError(Exception):
    @override
    def __str__(self) -> str:
        return "personal_send_pending"


async def authorize_dispatch(connection: PersonalConnection) -> PersonalSend | None:
    async with connection_ctx() as db:
        await personal_owner(connection.agent_id, authorization_only=True)
        current = await get_personal(connection.agent_id, lock=True)
        if (
            current is None
            or current.claim_token != connection.claim_token
            or not current.desired
            or current.generation != connection.generation
            or current.state != "connected"
        ):
            return None
        # Renew/auth validation owns the lease fence. No provider I/O occurs in this transaction.
        raw = await db.fetchone(
            "UPDATE whatsapp_personal_sends SET state='dispatch_started',claim_token=%(claim)s,dispatched_at=now() "
            "WHERE command_id=(SELECT command_id FROM whatsapp_personal_sends WHERE config_id=%(config)s "
            "AND auth_epoch=%(epoch)s AND generation=%(generation)s AND account_id=%(account)s "
            "AND state='pending' ORDER BY created_at,command_id LIMIT 1 FOR UPDATE) "
            "AND EXISTS(SELECT 1 FROM whatsapp_personal_connections WHERE config_id=%(config)s "
            "AND claim_token=%(claim)s AND lease_until>now()) RETURNING *",
            {
                "config": current.config_id,
                "claim": current.claim_token,
                "epoch": current.auth_epoch,
                "generation": current.generation,
                "account": current.account_id,
            },
        )
        return PersonalSend.model_validate(raw) if raw is not None else None
