"""ACL mutations serialize on ordered tenant rows, then subjects, then agents.

Use inside connection_ctx; these are transaction-scoped PostgreSQL row locks.
The tenant mutex prevents membership/grant races from missing affected agents.
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException

from app.core.access_cache import bump_agent_acl_version, clear_request_memo
from app.core.json_types import uuid_from_row
from app.core.row_memo import clear_entity_memo
from app.db.connection import DbConnection
from app.db.session import optional_connection_ctx


async def lock_tenants(db: DbConnection, ids: Iterable[UUID | None]) -> None:
    for tenant_id in sorted({item for item in ids if item is not None}):
        await db.fetchone("SELECT id FROM tenants WHERE id = %(id)s FOR UPDATE", {"id": tenant_id})
    clear_request_memo()
    clear_entity_memo()


async def lock_user(db: DbConnection, user_id: UUID, tenant_id: UUID | None) -> None:
    row = await db.fetchone("SELECT tenant_id FROM users WHERE id = %(id)s FOR UPDATE", {"id": user_id})
    if row is None:
        raise HTTPException(404, "User not found")
    current = uuid_from_row(row["tenant_id"]) if row["tenant_id"] is not None else None
    if current != tenant_id:
        raise HTTPException(409, "User organization changed; retry")


async def lock_agent(db: DbConnection, agent_id: UUID, tenant_id: UUID | None) -> None:
    row = await db.fetchone("SELECT tenant_id FROM agents WHERE id = %(id)s FOR UPDATE", {"id": agent_id})
    if row is None:
        raise HTTPException(404, "Agent not found")
    current = uuid_from_row(row["tenant_id"]) if row["tenant_id"] is not None else None
    if current != tenant_id:
        raise HTTPException(409, "Agent organization changed; retry")


async def invalidate_tenant_agents(db: DbConnection, tenant_id: UUID | None) -> None:
    clear_request_memo()
    rows = await db.fetchall(
        "SELECT id FROM agents WHERE tenant_id = %(tenant)s ORDER BY id FOR UPDATE", {"tenant": tenant_id}
    )
    for row in rows:
        await bump_agent_acl_version(uuid_from_row(row["id"]))


@asynccontextmanager
async def agent_acl_mutation(agent_id: UUID) -> AsyncIterator[None]:
    async with optional_connection_ctx() as db:
        if db is not None:
            row = await db.fetchone("SELECT tenant_id FROM agents WHERE id = %(id)s", {"id": agent_id})
            if row is not None:
                tenant_id = uuid_from_row(row["tenant_id"]) if row["tenant_id"] is not None else None
                await lock_tenants(db, (tenant_id,))
                await lock_agent(db, agent_id, tenant_id)
        yield
