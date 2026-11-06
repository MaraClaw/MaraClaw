"""ACL mutations serialize on ordered tenant rows, then subjects, then agents.

Use inside connection_ctx; these are transaction-scoped PostgreSQL row locks.
The tenant mutex prevents membership/grant races from missing affected agents.

``lock_tenants`` and ``lock_user`` take ``FOR UPDATE`` by default. ``shared=True`` takes ``FOR SHARE`` on tenants and
``FOR KEY SHARE`` on users: both exclude the ACL mutators' explicit ``FOR UPDATE`` locks. User quota updates remain
compatible, avoiding a cycle with receipt INSERTs' agent FK locks. Never upgrade these locks in the same transaction.
"""

from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import HTTPException

from app.core.access_cache import bump_agent_acl_version, clear_request_memo
from app.core.json_types import uuid_from_row
from app.core.row_memo import clear_entity_memo
from app.db.connection import DbConnection
from app.db.session import optional_connection_ctx

_LOCK_TENANT_SQL = {
    False: "SELECT id FROM tenants WHERE id = %(id)s FOR UPDATE",
    True: "SELECT id FROM tenants WHERE id = %(id)s FOR SHARE",
}
_LOCK_USER_SQL = {
    False: "SELECT tenant_id FROM users WHERE id = %(id)s FOR UPDATE",
    True: "SELECT tenant_id FROM users WHERE id = %(id)s FOR KEY SHARE",
}


async def lock_tenants(db: DbConnection, ids: Iterable[UUID | None], *, shared: bool = False) -> None:
    for tenant_id in sorted({item for item in ids if item is not None}):
        await db.fetchone(_LOCK_TENANT_SQL[shared], {"id": tenant_id})
    clear_request_memo()
    clear_entity_memo()


async def lock_user(db: DbConnection, user_id: UUID, tenant_id: UUID | None, *, shared: bool = False) -> None:
    row = await db.fetchone(_LOCK_USER_SQL[shared], {"id": user_id})
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
async def agent_acl_mutation(agent_id: UUID) -> AsyncGenerator[None]:
    async with optional_connection_ctx() as db:
        if db is not None:
            row = await db.fetchone("SELECT tenant_id FROM agents WHERE id = %(id)s", {"id": agent_id})
            if row is not None:
                tenant_id = uuid_from_row(row["tenant_id"]) if row["tenant_id"] is not None else None
                await lock_tenants(db, (tenant_id,))
                await lock_agent(db, agent_id, tenant_id)
        yield
