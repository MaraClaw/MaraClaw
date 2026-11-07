from dataclasses import dataclass
from uuid import UUID

from app.core.json_types import uuid_from_row_opt
from app.db.session import connection_ctx


@dataclass(frozen=True, slots=True)
class TavilyAgentAvailability:
    """An agent that may spend a key. ``tenant_id`` is None for an agent that belongs to no tenant."""

    tenant_id: UUID | None


async def save_proxy_token(agent_id: UUID, token_hash: bytes) -> None:
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO tavily_proxy_tokens (agent_id, token_hash) VALUES (%(id)s, %(hash)s) "
            "ON CONFLICT (agent_id) DO UPDATE SET token_hash=EXCLUDED.token_hash",
            {"id": agent_id, "hash": token_hash},
        )


async def agent_available(agent_id: UUID, token_hash: bytes) -> TavilyAgentAvailability | None:
    async with connection_ctx() as db:
        row = await db.fetchone(
            "SELECT a.tenant_id FROM agents a JOIN tavily_proxy_tokens t ON t.agent_id=a.id "
            "WHERE a.id=%(id)s AND t.token_hash=%(hash)s AND NOT a.is_expired "
            "AND (a.expires_at IS NULL OR a.expires_at > now()) "
            "AND NOT EXISTS (SELECT 1 FROM company_transfer_retained_agents r WHERE r.agent_id=a.id)",
            {"id": agent_id, "hash": token_hash},
        )
    return None if row is None else TavilyAgentAvailability(tenant_id=uuid_from_row_opt(row["tenant_id"]))
