from datetime import UTC, datetime
from typing import Literal, assert_never
from uuid import UUID

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.dao.zalo_personal_dao import fenced, get_personal
from app.db.session import connection_ctx
from app.records.outbound_plan import OutboundChunk, OutboundFileChunk
from app.records.zalo_personal import ZaloConnection, ZaloMediaSend, ZaloRoute, ZaloSend
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


def route_state(
    connection: ZaloConnection | None, route: ZaloRoute, now: datetime
) -> Literal["live", "recovering", "revoked"]:
    if (
        connection is None
        or connection.config_id != route.config_id
        or connection.auth_epoch != route.auth_epoch
        or connection.generation != route.generation
        or connection.account_id != route.account_id
        or not connection.desired
        or connection.purge_requested
        or connection.state not in {"connected", "reconnecting"}
    ):
        return "revoked"
    if (
        connection.state == "connected"
        and connection.claim_token is not None
        and connection.lease_until is not None
        and connection.lease_until > now
    ):
        return "live"
    return "recovering"


async def _queue(
    route: ZaloRoute, chunk: OutboundChunk | OutboundFileChunk, *, notice: bool
) -> ZaloSend | ZaloMediaSend:
    from app.services.channels.sender_links import is_bound

    match chunk:
        case OutboundChunk():
            if not chunk.text.strip() or len(chunk.text) > 2000:
                raise PersonalSendTerminalError("zalo_text_limit")
            find = "SELECT * FROM zalo_personal_sends WHERE command_id=%(id)s"
            conflict = "SELECT TRUE FROM zalo_personal_media_sends WHERE command_id=%(id)s"
        case OutboundFileChunk():
            find = "SELECT * FROM zalo_personal_media_sends WHERE command_id=%(id)s"
            conflict = "SELECT TRUE FROM zalo_personal_sends WHERE command_id=%(id)s"
        case unreachable:
            assert_never(unreachable)
    async with connection_ctx() as db:
        # Resolve by immutable config without acquiring its row before the owner locks.
        raw = await db.fetchone(
            "SELECT * FROM zalo_personal_connections WHERE config_id=%(id)s", {"id": route.config_id}
        )
        if raw is None:
            raise PersonalSendTerminalError("zalo_route_revoked")
        current = ZaloConnection.from_row(raw)
        agent, owner = await zalo_owner(current.agent_id, authorization_only=True)
        require_running(agent)
        current = await get_personal(agent.id, lock=True)
        state = route_state(current, route, datetime.now(UTC))
        if (
            current is None
            or current.owner_id != owner.id
            or current.tenant_id != agent.tenant_id
            or state == "revoked"
            or route.peer_id == route.account_id
        ):
            raise PersonalSendTerminalError("zalo_route_revoked")
        if not notice and not await is_bound(route.config_id, route.account_id, route.peer_id):
            raise PersonalSendTerminalError("zalo_peer_unbound")
        recovering = state == "recovering"
        if not recovering:
            try:
                current = await fenced(current, connected=True)
            except HTTPException as error:
                if error.status_code != 409:
                    raise
                latest = await get_personal(agent.id, lock=True)
                if (
                    latest is None
                    or latest.owner_id != owner.id
                    or latest.tenant_id != agent.tenant_id
                    or route_state(latest, route, datetime.now(UTC)) != "recovering"
                ):
                    raise
                recovering = True
            else:
                if not route_matches(current, route) or current.purge_requested:
                    raise PersonalSendTerminalError("zalo_route_revoked")
        # A command id belongs to exactly one payload kind, even across configurations.
        await db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%(key)s,0))", {"key": str(chunk.key)})
        if await db.fetchval(conflict, {"id": chunk.key}):
            raise PersonalSendTerminalError("personal_target_changed")
        if not recovering and isinstance(chunk, OutboundChunk):
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
        if not recovering and isinstance(chunk, OutboundFileChunk):
            await db.execute(
                "INSERT INTO zalo_personal_media_sends(command_id,config_id,auth_epoch,generation,account_id,peer_id,file) "
                "VALUES(%(command)s,%(config)s,%(epoch)s,%(generation)s,%(account)s,%(peer)s,%(file)s) "
                "ON CONFLICT(command_id) DO NOTHING",
                {
                    "command": chunk.key,
                    "config": route.config_id,
                    "epoch": route.auth_epoch,
                    "generation": route.generation,
                    "account": route.account_id,
                    "peer": route.peer_id,
                    "file": Jsonb(chunk.file.model_dump(mode="json")),
                },
            )
        raw = await db.fetchone(find, {"id": chunk.key})
        if recovering and raw is None:
            raise PersonalSendPendingError
        row = (
            ZaloMediaSend.model_validate(raw) if isinstance(chunk, OutboundFileChunk) else ZaloSend.model_validate(raw)
        )
        match row, chunk:
            case ZaloSend(), OutboundChunk():
                same_payload = row.text == chunk.text and row.purpose == ("link_notice" if notice else "reply")
            case ZaloMediaSend(), OutboundFileChunk():
                same_payload = row.file == chunk.file
            case _:
                same_payload = False
        if (
            row.config_id != route.config_id
            or row.auth_epoch != route.auth_epoch
            or row.generation != route.generation
            or row.account_id != route.account_id
            or row.peer_id != route.peer_id
            or not same_payload
        ):
            raise PersonalSendTerminalError("personal_target_changed")
        return row


async def enqueue(destination: str, chunk: OutboundChunk | OutboundFileChunk) -> None:
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
        collision = await db.fetchval(
            "SELECT TRUE FROM zalo_personal_media_sends WHERE command_id=%(id)s", {"id": row.command_id}
        )
        bound = not collision and (
            row.purpose == "link_notice" or await is_bound(row.config_id, row.account_id, row.peer_id)
        )
        updated = await db.fetchone(
            "UPDATE zalo_personal_sends SET state=%(state)s,claim_token=%(claim)s,dispatched_at=now(),"
            "finished_at=CASE WHEN %(state)s='cancelled' THEN now() ELSE NULL END WHERE command_id=%(id)s RETURNING *",
            {"state": "dispatch_started" if bound else "cancelled", "claim": current.claim_token, "id": row.command_id},
        )
        return ZaloSend.model_validate(updated) if bound else None


async def record_result(connection: ZaloConnection, command_id: UUID, outcome: str, message_id: str) -> None:
    from app.services.channels.zalo_personal_media_journal import record_media_result

    if outcome not in {"accepted", "rejected", "uncertain"} or (outcome == "accepted" and not message_id.strip()):
        raise ValueError("zalo_send_result_invalid")
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
        await record_media_result(connection, command_id, outcome, message_id)


async def pending_media(connection: ZaloConnection) -> ZaloMediaSend | None:
    from app.services.channels.zalo_personal_media_journal import pending_media as pending

    return await pending(connection)


async def authorize_media_dispatch(connection: ZaloConnection, command_id: UUID) -> ZaloMediaSend | None:
    from app.services.channels.zalo_personal_media_journal import authorize_media_dispatch as authorize

    return await authorize(connection, command_id)
