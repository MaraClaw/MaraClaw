from uuid import UUID

from app.db.session import connection_ctx


async def agent_available(agent_id: UUID) -> bool:
    async with connection_ctx() as db:
        return bool(
            await db.fetchval(
                "SELECT EXISTS (SELECT 1 FROM agents a WHERE a.id=%(id)s AND NOT a.is_expired "
                "AND (a.expires_at IS NULL OR a.expires_at > now()) "
                "AND NOT EXISTS (SELECT 1 FROM company_transfer_retained_agents r WHERE r.agent_id=a.id))",
                {"id": agent_id},
            )
        )
