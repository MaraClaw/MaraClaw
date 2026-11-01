import base64
import re
from datetime import UTC, datetime
from typing import assert_never, override
from uuid import UUID

from fastapi import HTTPException
from pydantic import JsonValue, TypeAdapter

from app.config import get_settings
from app.core.logging import logger
from app.dao.whatsapp_personal_auth_dao import AuthMutation, read_auth, write_auth, write_auth_batch
from app.dao.whatsapp_personal_dao import get_personal, renew, revoke
from app.db.session import connection_ctx
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_access import personal_owner
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey
from app.services.channels.whatsapp_personal_intake import accept_message
from app.services.channels.whatsapp_personal_protocol import (
    QR,
    AuthGet,
    AuthSet,
    ChildFrame,
    Closed,
    CredsSet,
    Identity,
    Message,
    Ready,
    SendResult,
)

_json = TypeAdapter(JsonValue)
_creds = TypeAdapter(dict[str, JsonValue])


class IdentityAmbiguousError(Exception):
    @override
    def __str__(self) -> str:
        return "identity_ambiguous"


async def handle_frame(connection: PersonalConnection, frame: ChildFrame) -> JsonValue:
    match frame:
        case AuthGet():
            values = await read_auth(connection, frame.category, frame.ids)
            return {key: _json.validate_json(value) for key, value in values.items()}
        case AuthSet():
            await write_auth_batch(connection, [AuthMutation(batch.category, batch.values) for batch in frame.batches])
        case CredsSet():
            _creds.validate_json(frame.creds)
            await write_auth(connection, AuthMutation("creds", {"main": frame.creds}))
        case Identity():
            await bind_identity(connection, frame)
        case QR():
            async with connection_ctx() as db:
                await personal_owner(connection.agent_id)
                if not await renew(connection):
                    raise HTTPException(409, "personal_claim_revoked")
                await db.execute(
                    "UPDATE whatsapp_personal_connections SET qr_value=%(value)s,qr_expires_at=LEAST(%(expires)s,"
                    "expires_at),state='qr',revision=revision+1 WHERE config_id=%(id)s AND attempt_id=%(attempt)s "
                    "AND expires_at>now()",
                    {
                        "id": connection.config_id,
                        "attempt": connection.attempt_id,
                        "value": base64.b64encode(
                            AuthCipher(get_settings().SECRET_KEY).seal(
                                AuthKey(connection.config_id, connection.auth_epoch, "qr", str(connection.attempt_id)),
                                frame.value,
                            )
                        ).decode("ascii"),
                        "expires": frame.expires_at,
                    },
                )
        case Message():
            async with connection_ctx():
                current = await get_personal(connection.agent_id)
            if current is not None and current.claim_token == connection.claim_token:
                await accept_message(current, frame)
        case SendResult():
            async with connection_ctx() as db:
                await db.execute(
                    "UPDATE whatsapp_personal_sends SET state=%(state)s,finished_at=now() WHERE command_id=%(id)s "
                    "AND config_id=%(config)s AND claim_token=%(claim)s AND message_id=%(message)s "
                    "AND state='dispatch_started'",
                    {
                        "state": frame.outcome,
                        "id": UUID(frame.command_id),
                        "config": connection.config_id,
                        "claim": connection.claim_token,
                        "message": frame.message_id,
                    },
                )
        case Closed():
            if frame.reason in {"logged_out", "replaced", "forbidden", "auth_store_failed", "transport_failed"}:
                await revoke_current_claim(connection, frame.reason)
        case Ready():
            pass
        case unreachable:
            assert_never(unreachable)
    return None


async def revoke_current_claim(connection: PersonalConnection, reason: str) -> None:
    async with connection_ctx() as db:
        try:
            await personal_owner(connection.agent_id)
        except HTTPException:
            # Membership loss still requires revocation; ownership locks acquired before validation remain held.
            logger.debug("[WhatsAppPersonal] owner_unavailable")
        current = await get_personal(connection.agent_id, lock=True)
        if (
            current is None
            or current.generation != connection.generation
            or current.auth_epoch != connection.auth_epoch
            or current.claim_token != connection.claim_token
        ):
            return
        await revoke(connection.agent_id, purge=reason != "transport_failed")
        await db.execute(
            "UPDATE whatsapp_personal_connections SET error_code=%(code)s "
            "WHERE config_id=%(id)s AND claim_token=%(claim)s",
            {
                "id": connection.config_id,
                "claim": connection.claim_token,
                "code": reason if reason in {"auth_store_failed", "transport_failed"} else "logged_out",
            },
        )


async def bind_identity(connection: PersonalConnection, identity: Identity) -> None:
    pn, lid = identity.pn_jid, identity.lid_jid
    if (
        pn is None
        or re.fullmatch(r"[0-9]+@s\.whatsapp\.net", pn) is None
        or (lid is not None and re.fullmatch(r"[0-9]+@lid", lid) is None)
        or identity.account_id != pn
    ):
        raise IdentityAmbiguousError
    async with connection_ctx() as db:
        await personal_owner(connection.agent_id)
        current = await get_personal(connection.agent_id, lock=True)
        if current is None or not await renew(connection):
            raise HTTPException(409, "personal_claim_revoked")
        if current.account_id is not None and current.account_id != identity.account_id:
            raise IdentityAmbiguousError
        if (current.pn_jid is not None and current.pn_jid != pn) or (
            current.lid_jid is not None and lid is not None and current.lid_jid != lid
        ):
            raise IdentityAmbiguousError
        await db.execute(
            "UPDATE whatsapp_personal_connections SET account_id=%(account)s,pn_jid=%(pn)s,lid_jid=%(lid)s,"
            "state='connected',qr_value=NULL,qr_expires_at=NULL,expires_at=NULL,activated_at=COALESCE(activated_at,"
            "%(now)s),revision=revision+1 WHERE config_id=%(id)s",
            {
                "id": current.config_id,
                "account": identity.account_id,
                "pn": pn or current.pn_jid,
                "lid": lid or current.lid_jid,
                "now": datetime.now(UTC),
            },
        )
        await db.execute("UPDATE channel_configs SET is_connected=TRUE WHERE id=%(id)s", {"id": current.config_id})
