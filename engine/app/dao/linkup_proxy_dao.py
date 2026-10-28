from uuid import UUID

from app.db.session import connection_ctx


async def save_proxy_token(agent_id: UUID, token_hash: bytes) -> None:
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO linkup_proxy_tokens (agent_id, token_hash) VALUES (%(id)s, %(hash)s) "
            "ON CONFLICT (agent_id) DO UPDATE SET token_hash=EXCLUDED.token_hash",
            {"id": agent_id, "hash": token_hash},
        )


async def agent_available(agent_id: UUID, token_hash: bytes) -> bool:
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agents a JOIN linkup_proxy_tokens t ON t.agent_id=a.id "
                "WHERE a.id=%(id)s AND t.token_hash=%(hash)s AND NOT a.is_expired "
                "AND (a.expires_at IS NULL OR a.expires_at > now()) "
                "AND NOT EXISTS (SELECT 1 FROM company_transfer_retained_agents r WHERE r.agent_id=a.id))",
                {"id": agent_id, "hash": token_hash},
            )
        )
