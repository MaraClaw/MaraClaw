from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, JsonValue

from app.config import get_settings
from app.db.session import connection_ctx
from app.records.whatsapp_personal import PersonalConnection
from app.services.channels.whatsapp_personal_crypto import AuthCipher, AuthKey


class _EncryptedRow(BaseModel):
    key_id: str
    ciphertext: bytes


@dataclass(frozen=True, slots=True)
class AuthMutation:
    category: str
    values: dict[str, JsonValue]


async def _fence(connection: PersonalConnection) -> None:
    async with connection_ctx() as db:
        valid = await db.fetchval(
            "SELECT TRUE FROM whatsapp_personal_connections WHERE config_id=%(id)s AND auth_epoch=%(epoch)s "
            "AND generation=%(generation)s AND claim_token=%(claim)s AND desired AND lease_until>now() FOR UPDATE",
            {
                "id": connection.config_id,
                "epoch": connection.auth_epoch,
                "generation": connection.generation,
                "claim": connection.claim_token,
            },
        )
        if not valid:
            raise HTTPException(409, "auth_store_failed")


async def read_auth(connection: PersonalConnection, category: str, ids: list[str]) -> dict[str, str]:
    async with connection_ctx() as db:
        await _fence(connection)
        rows = await db.fetchall(
            "SELECT key_id,ciphertext FROM whatsapp_personal_auth WHERE config_id=%(id)s "
            "AND auth_epoch=%(epoch)s AND category=%(category)s AND key_id=ANY(%(ids)s)",
            {"id": connection.config_id, "epoch": connection.auth_epoch, "category": category, "ids": ids},
        )
        cipher = AuthCipher(get_settings().SECRET_KEY)
        result: dict[str, str] = {}
        for raw in rows:
            row = _EncryptedRow.model_validate(raw)
            result[row.key_id] = cipher.open(
                AuthKey(connection.config_id, connection.auth_epoch, category, row.key_id), row.ciphertext
            )
        return result


async def write_auth(connection: PersonalConnection, mutation: AuthMutation) -> None:
    import json

    async with connection_ctx() as db:
        await _fence(connection)
        cipher = AuthCipher(get_settings().SECRET_KEY)
        for key_id, value in mutation.values.items():
            params: dict[str, UUID | str | bytes] = {
                "id": connection.config_id,
                "epoch": connection.auth_epoch,
                "category": mutation.category,
                "key": key_id,
            }
            if value is None:
                await db.execute(
                    "DELETE FROM whatsapp_personal_auth WHERE config_id=%(id)s AND auth_epoch=%(epoch)s "
                    "AND category=%(category)s AND key_id=%(key)s",
                    params,
                )
            else:
                plaintext = value if mutation.category == "creds" and isinstance(value, str) else json.dumps(value)
                params["ciphertext"] = cipher.seal(
                    AuthKey(connection.config_id, connection.auth_epoch, mutation.category, key_id), plaintext
                )
                await db.execute(
                    "INSERT INTO whatsapp_personal_auth(config_id,auth_epoch,category,key_id,ciphertext) "
                    "VALUES(%(id)s,%(epoch)s,%(category)s,%(key)s,%(ciphertext)s) "
                    "ON CONFLICT(config_id,auth_epoch,category,key_id) DO UPDATE SET ciphertext=EXCLUDED.ciphertext",
                    params,
                )


async def write_auth_batch(connection: PersonalConnection, mutations: Sequence[AuthMutation]) -> None:
    """Apply every mutation in one transaction; nested ``write_auth`` scopes join it, so any failure rolls all back."""
    async with connection_ctx():
        for mutation in mutations:
            await write_auth(connection, mutation)
