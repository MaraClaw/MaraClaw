from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI, Header

from app.api import notification
from app.core.security import get_current_user
from app.dao.notification_dao import notification_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.session import bind_crud_connection
from app.records.notification import NotificationRecord
from app.records.user import UserRecord
from app.scripts import bootstrap_db
from test_bootstrap_postgres import schema_db as schema_db


@dataclass(frozen=True, slots=True)
class ArchiveScenario:
    db: DbConnection
    client: httpx.AsyncClient
    owner: UserRecord
    colleague: UserRecord
    outsider: UserRecord

    def headers(self, user: UserRecord) -> dict[str, str]:
        return {"x-test-user": str(user.id)}

    async def notice(self, *, is_read: bool = False, kind: str = "broadcast") -> NotificationRecord:
        return await notification_dao.create(
            obj_in={"user_id": self.owner.id, "type": kind, "title": "Notice", "is_read": is_read}
        )


@pytest.fixture
async def archive_scenario(schema_db: DbConnection) -> AsyncIterator[ArchiveScenario]:
    # The existing fixture owns a unique PostgreSQL schema and drops it on exit.
    await bootstrap_db.main()
    await bootstrap_db.main()
    await bootstrap_db.init_pool()
    tenant = await tenant_dao.create(obj_in={"name": "Owner", "slug": "owner"})
    other_tenant = await tenant_dao.create(obj_in={"name": "Other", "slug": "other"})
    owner = await user_dao.create(obj_in={"tenant_id": tenant.id, "display_name": "Owner"})
    colleague = await user_dao.create(obj_in={"tenant_id": tenant.id, "display_name": "Colleague"})
    outsider = await user_dao.create(obj_in={"tenant_id": other_tenant.id, "display_name": "Outsider"})
    users = {user.id: user for user in (owner, colleague, outsider)}

    async def current_user(x_test_user: Annotated[UUID, Header()]) -> UserRecord:
        return users[x_test_user]

    app = FastAPI()
    app.include_router(notification.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.dependency_overrides[get_current_user] = current_user
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://archive.test") as client:
            yield ArchiveScenario(schema_db, client, owner, colleague, outsider)
    finally:
        app.dependency_overrides.clear()
