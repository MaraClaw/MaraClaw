from uuid import UUID

from fastapi import HTTPException

from app.dao.zalo_personal_dao import fenced, get_personal
from app.db.session import connection_ctx
from app.records.zalo_personal import ZaloSource


async def authorize_zalo_source(source: ZaloSource, agent_id: UUID) -> None:
    async with connection_ctx() as db:
        current = await get_personal(agent_id)
        if current is None:
            raise HTTPException(403, "Zalo source unavailable")
        current = await fenced(current, connected=True)
        if (
            source.config_id != current.config_id
            or source.auth_epoch != current.auth_epoch
            or source.generation != current.generation
            or source.claim_token != current.claim_token
            or source.account_id != current.account_id
        ):
            raise HTTPException(403, "Zalo source revoked")
        if await db.fetchval(
            "SELECT TRUE FROM zalo_personal_sends WHERE account_id=%(account)s AND message_id=%(message)s LIMIT 1",
            {"account": source.account_id, "message": source.message_id},
        ):
            raise HTTPException(403, "Zalo assistant echo")
