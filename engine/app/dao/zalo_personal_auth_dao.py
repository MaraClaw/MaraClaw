from pydantic import BaseModel

from app.config import get_settings
from app.dao.zalo_personal_dao import fenced
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloConnection
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey

PURPOSE = b"maraclaw:zalo:auth:v1"


class _SealedAuth(BaseModel):
    ciphertext: bytes


async def read_auth(connection: ZaloConnection) -> str | None:
    async with connection_ctx() as db:
        await fenced(connection)
        raw = await db.fetchone(
            "SELECT ciphertext FROM zalo_personal_auth WHERE config_id=%(id)s AND auth_epoch=%(epoch)s",
            {"id": connection.config_id, "epoch": connection.auth_epoch},
        )
        if raw is None:
            return None
        return AuthCipher(get_settings().SECRET_KEY, purpose=PURPOSE).open(
            AuthKey(connection.config_id, connection.auth_epoch, "creds", "main"),
            _SealedAuth.model_validate(raw).ciphertext,
        )


async def write_auth(connection: ZaloConnection, credentials: str) -> None:
    async with connection_ctx() as db:
        await fenced(connection)
        sealed = AuthCipher(get_settings().SECRET_KEY, purpose=PURPOSE).seal(
            AuthKey(connection.config_id, connection.auth_epoch, "creds", "main"),
            credentials,
        )
        await db.execute(
            "INSERT INTO zalo_personal_auth(config_id,auth_epoch,ciphertext) VALUES(%(id)s,%(epoch)s,%(sealed)s) "
            "ON CONFLICT(config_id,auth_epoch) DO UPDATE SET ciphertext=EXCLUDED.ciphertext,updated_at=now()",
            {"id": connection.config_id, "epoch": connection.auth_epoch, "sealed": sealed},
        )
