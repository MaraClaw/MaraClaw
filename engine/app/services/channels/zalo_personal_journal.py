from uuid import UUID

from fastapi import HTTPException

from app.dao.zalo_personal_dao import fenced
from app.db.session import connection_ctx
from app.records.outbound_plan import OutboundChunk
from app.records.zalo_personal import ZaloConnection, ZaloRoute, ZaloSend
from app.services.channels.whatsapp_personal_journal import PersonalSendPendingError, PersonalSendTerminalError
from app.services.channels.zalo_personal_access import require_running, zalo_owner


def route_matches(connection: ZaloConnection, route: ZaloRoute) -> bool:
    return (
        connection.config_id == route.config_id
        and connection.auth_epoch == route.auth_epoch
        and connection.generation == route.generation
        and connection.account_id == route.account_id
        and connection.desired
        and connection.state == "connected"
    )


async def _queue(route: ZaloRoute, chunk: OutboundChunk, *, notice: bool) -> ZaloSend:
    from app.services.channels.sender_links import is_bound

    if not chunk.text.strip() or len(chunk.text) > 2000:
        raise PersonalSendTerminalError("zalo_text_limit")
    async with connection_ctx() as db:
        # Resolve by immutable config without acquiring its row before the owner locks.
        raw = await db.fetchone(
            "SELECT * FROM zalo_personal_connections WHERE config_id=%(id)s", {"id": route.config_id}
        )
        if raw is None:
            raise PersonalSendTerminalError("zalo_route_revoked")
        current = ZaloConnection.from_row(raw)
        agent, _ = await zalo_owner(current.agent_id, authorization_only=True)
        require_running(agent)
        current = await fenced(current, connected=True)
        if not route_matches(current, route) or route.peer_id == route.account_id:
            raise PersonalSendTerminalError("zalo_route_revoked")
        if not notice and not await is_bound(route.config_id, route.account_id, route.peer_id):
            raise PersonalSendTerminalError("zalo_peer_unbound")
        await db.execute(
            "INSERT INTO zalo_personal_sends(command_id,config_id,auth_epoch,generation,account_id,peer_id,text,purpose) "
            "VALUES(%(command)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,%(peer)s,%(text)s,%(purpose)s) "
            "ON CONFLICT(command_id) DO NOTHING",
            {
                "command": chunk.key,
                "config": route.config_id,
                "epoch": route.auth_epoch,
                "generation": route.generation,
                "account": route.account_id,
                "peer": route.peer_id,
                "text": chunk.text,
                "purpose": "link_notice" if notice else "reply",
            },
        )
        row = ZaloSend.model_validate(
            await db.fetchone("SELECT * FROM zalo_personal_sends WHERE command_id=%(id)s", {"id": chunk.key})
        )
        if (
            row.config_id != route.config_id
            or row.auth_epoch != route.auth_epoch
            or row.generation != route.generation
            or row.account_id != route.account_id
            or row.peer_id != route.peer_id
            or row.text != chunk.text
            or row.purpose != ("link_notice" if notice else "reply")
        ):
            raise PersonalSendTerminalError("personal_target_changed")
        return row


async def enqueue(destination: str, chunk: OutboundChunk) -> None:
    try:
        route = ZaloRoute.from_external_id(destination)
        row = await _queue(route, chunk, notice=False)
    except ValueError, HTTPException:
        raise PersonalSendTerminalError("zalo_route_invalid") from None
    match row.state:
        case "accepted":
            return
        case "rejected" | "uncertain" | "cancelled":
            raise PersonalSendTerminalError("personal_send_" + row.state)
        case "pending" | "dispatch_started":
            raise PersonalSendPendingError


async def queue_link_notice(route: ZaloRoute, chunk: OutboundChunk) -> None:
    await _queue(route, chunk, notice=True)


async def authorize_dispatch(connection: ZaloConnection) -> ZaloSend | None:
    from app.services.channels.sender_links import is_bound

    async with connection_ctx() as db:
        current = await fenced(connection, connected=True)
        raw = await db.fetchone(
            "SELECT * FROM zalo_personal_sends WHERE config_id=%(config)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND account_id=%(account)s AND state='pending' "
            "ORDER BY created_at,command_id LIMIT 1 FOR UPDATE",
            {
                "config": current.config_id,
                "epoch": current.auth_epoch,
                "generation": current.generation,
                "account": current.account_id,
            },
        )
        if raw is None:
            return None
        row = ZaloSend.model_validate(raw)
        bound = row.purpose == "link_notice" or await is_bound(row.config_id, row.account_id, row.peer_id)
        updated = await db.fetchone(
            "UPDATE zalo_personal_sends SET state=%(state)s,claim_token=%(claim)s,dispatched_at=now(),"
            "finished_at=CASE WHEN %(state)s='cancelled' THEN now() ELSE NULL END WHERE command_id=%(id)s RETURNING *",
            {"state": "dispatch_started" if bound else "cancelled", "claim": current.claim_token, "id": row.command_id},
        )
        return ZaloSend.model_validate(updated) if bound else None


async def record_result(connection: ZaloConnection, command_id: UUID, outcome: str, message_id: str) -> None:
    async with connection_ctx() as db:
        await fenced(connection, connected=True)
        await db.execute(
            "UPDATE zalo_personal_sends SET state=%(outcome)s,message_id=%(message)s,finished_at=now() "
            "WHERE command_id=%(command)s AND config_id=%(config)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND claim_token=%(claim)s AND state='dispatch_started'",
            {
                "outcome": outcome,
                "message": message_id or None,
                "command": command_id,
                "config": connection.config_id,
                "epoch": connection.auth_epoch,
                "generation": connection.generation,
                "claim": connection.claim_token,
            },
        )
