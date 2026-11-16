from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from app.config import get_settings
from app.dao.zalo_personal_auth_dao import PURPOSE, write_auth
from app.dao.zalo_personal_dao import fail, fenced
from app.dao.zalo_personal_runtime_dao import interrupted, observe
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey
from app.services.channels.zalo_personal_journal import record_result
from app.services.channels.zalo_personal_protocol import (
    QR,
    ChildFrame,
    Closed,
    CredsSet,
    Heartbeat,
    Identity,
    MediaTransfer,
    Message,
    Ready,
    SendResult,
)


async def handle_frame(
    connection: ZaloConnection, frame: ChildFrame, *, download: Callable[[Message], Awaitable[bytes]] | None = None
) -> None:
    match frame:
        case CredsSet():
            await write_auth(connection, frame.creds)
        case Message():
            from app.services.channels.zalo_personal_intake import intake

            if frame.media is not None:
                await intake(connection, frame, download=download)
                return
            async with connection_ctx() as db:
                receipt_query = (
                    "SELECT TRUE FROM channel_event_receipts WHERE config_id=%(config)s AND provider='zalo' "
                    "AND event_key=%(event)s"
                )
                receipt_key = {
                    "config": connection.config_id,
                    "event": f"personal:{connection.auth_epoch}:{frame.message_id}",
                }
                existed = await db.fetchval(receipt_query, receipt_key)
                await intake(connection, frame)
                if not existed and await db.fetchval(receipt_query, receipt_key):
                    await observe(connection, "activity")
        case SendResult():
            async with connection_ctx() as db:
                await fenced(connection, connected=True)
                dispatched = await db.fetchval(
                    "SELECT TRUE FROM (SELECT command_id,config_id,auth_epoch,generation,claim_token,state "
                    "FROM zalo_personal_sends UNION ALL SELECT command_id,config_id,auth_epoch,generation,claim_token,state "
                    "FROM zalo_personal_media_sends) sends WHERE command_id=%(command)s AND config_id=%(config)s "
                    "AND auth_epoch=%(epoch)s AND generation=%(generation)s AND claim_token=%(claim)s "
                    "AND state='dispatch_started'",
                    {
                        "command": UUID(frame.command_id),
                        "config": connection.config_id,
                        "epoch": connection.auth_epoch,
                        "generation": connection.generation,
                        "claim": connection.claim_token,
                    },
                )
                await record_result(connection, UUID(frame.command_id), frame.outcome, frame.message_id)
                if dispatched and frame.outcome == "accepted":
                    await observe(connection, "activity")
        case Heartbeat():
            await observe(connection, "heartbeat", ok=frame.ok)
        case MediaTransfer():
            raise ValueError("unexpected_media_frame")
        case Closed():
            match frame.reason:
                case "shutdown":
                    return
                case "transport_failed" | "auth_store_failed" | "lease_expired" | "session_replaced":
                    await interrupted(connection, frame.reason)
                case "protocol_error" | "login_failed" | "qr_expired" | "qr_declined":
                    await fail(connection, frame.reason)
        case QR():
            async with connection_ctx() as db:
                current = await fenced(connection)
                if (
                    current.state == "connected"
                    or current.account_id is not None
                    or frame.expires_at <= datetime.now(UTC)
                ):
                    raise ValueError("unexpected_qr")
                expires = min(frame.expires_at, current.expires_at) if current.expires_at else frame.expires_at
                sealed = AuthCipher(get_settings().SECRET_KEY, purpose=PURPOSE).seal(
                    AuthKey(current.config_id, current.auth_epoch, "qr", str(current.attempt_id)),
                    frame.value,
                )
                await db.execute(
                    "UPDATE zalo_personal_connections SET state='qr',qr_value=%(qr)s,"
                    "qr_expires_at=%(expires)s,expires_at=%(expires)s,revision=revision+1,updated_at=now() "
                    "WHERE config_id=%(id)s",
                    {"id": current.config_id, "qr": sealed, "expires": expires},
                )
        case Identity():
            async with connection_ctx() as db:
                current = await fenced(connection)
                if current.account_id is not None and current.account_id != frame.account_id:
                    raise ValueError("identity_changed")
                if not await db.fetchval(
                    "SELECT TRUE FROM zalo_personal_auth WHERE config_id=%(id)s AND auth_epoch=%(epoch)s",
                    {"id": current.config_id, "epoch": current.auth_epoch},
                ):
                    raise ValueError("credentials_missing")
                await db.execute(
                    "UPDATE zalo_personal_connections SET account_id=%(account)s,qr_value=NULL,"
                    "state=CASE WHEN state='connected' THEN state ELSE 'reconnecting' END,"
                    "qr_expires_at=NULL,revision=revision+1,updated_at=now() WHERE config_id=%(id)s",
                    {"id": current.config_id, "account": frame.account_id},
                )
        case Ready():
            async with connection_ctx() as db:
                current = await fenced(connection)
                if current.account_id is None or current.state not in {"reconnecting", "connected"}:
                    raise ValueError("identity_missing")
                if current.state == "connected":
                    return
                await db.execute(
                    "UPDATE zalo_personal_connections SET state='connected',activated_at=now(),"
                    "expires_at=NULL,qr_value=NULL,qr_expires_at=NULL,error_code=NULL,"
                    "revision=revision+1,updated_at=now() WHERE config_id=%(id)s",
                    {"id": current.config_id},
                )
                changed = await db.fetchval(
                    "UPDATE channel_configs SET app_id=%(account)s,is_connected=TRUE,updated_at=now() "
                    "WHERE id=%(id)s AND agent_id=%(agent)s AND channel_type='zalo' RETURNING TRUE",
                    {"id": current.config_id, "agent": current.agent_id, "account": current.account_id},
                )
                if not changed:
                    raise HTTPException(409, "Zalo configuration removed")
                await observe(current, "ready")
