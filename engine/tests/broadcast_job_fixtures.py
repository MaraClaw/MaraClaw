from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI

from app.api import notification as api
from app.core.domain.users import UserRole
from app.core.security import get_current_user
from app.db.session import bind_crud_connection
from app.records.user import UserRecord
from app.schemas.notification_responses import NotificationBroadcastJobAcceptanceResponse
from broadcast_email_persistence_fixtures import PersistenceDB, member, tenant

PATH = "/api/notifications/broadcast/jobs"


async def admin(db: PersistenceDB, count: int = 0) -> UserRecord:
    tenant_id = await tenant(db)
    sender = await member(db, tenant_id)
    sender.role = UserRole.ORG_ADMIN
    await db.admin.execute("UPDATE users SET role = 'org_admin' WHERE id = %(id)s", {"id": sender.id})
    for _ in range(count):
        _ = await member(db, tenant_id)
    return sender


def application(sender: UserRecord | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    if sender is not None:
        app.dependency_overrides[get_current_user] = lambda: sender
    return app


@asynccontextmanager
async def client_for(sender: UserRecord) -> AsyncGenerator[httpx.AsyncClient]:
    app = application(sender)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def accept(
    sender: UserRecord, key: str = "draft-1", *, email: bool = False
) -> NotificationBroadcastJobAcceptanceResponse:
    return await api.create_broadcast_job(
        api.BroadcastRequest(title="Update", body="Details", send_email=email), key, current_user=sender
    )


async def agent(db: PersistenceDB, sender: UserRecord) -> UUID:
    agent_id = uuid4()
    await db.admin.execute(
        """INSERT INTO agents
           (id, name, role_description, creator_id, tenant_id, max_triggers, min_poll_interval_min,
            webhook_rate_limit, access_mode, company_access_level, heartbeat_interval_minutes)
           VALUES (%(id)s, 'A', '', %(sender)s, %(tenant)s, 1, 1, 1, 'private', 'use', 30)""",
        {"id": agent_id, "sender": sender.id, "tenant": sender.tenant_id},
    )
    return agent_id
