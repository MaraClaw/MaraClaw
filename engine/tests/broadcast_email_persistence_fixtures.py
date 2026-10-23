import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.dao.broadcast_email_dao import broadcast_email_dao as outbox
from app.db.connection import DbConnection
from app.db.pool import EnginePool, close_pool, init_pool
from app.records.broadcast_email import BroadcastEmailRecord, NewBroadcastEmail
from app.records.user import UserRecord
from app.scripts import bootstrap_db


@dataclass(frozen=True, slots=True)
class PersistenceDB:
    admin: DbConnection
    conninfo: str

    async def restart(self) -> None:
        await close_pool()
        _ = await init_pool(conninfo=self.conninfo, min_size=1, max_size=4)


@pytest.fixture
async def broadcast_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PersistenceDB]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"broadcast_persistence_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        admin = DbConnection(raw)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")
        try:
            await admin.execute(f'SET search_path TO "{schema}"')

            async def bootstrap_pool() -> EnginePool:
                return await init_pool(conninfo=isolated, min_size=1, max_size=4)

            monkeypatch.setattr(bootstrap_db, "init_pool", bootstrap_pool)
            await bootstrap_db.main()
            _ = await init_pool(conninfo=isolated, min_size=1, max_size=4)
            yield PersistenceDB(admin, isolated)
        finally:
            await close_pool()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def tenant(db: PersistenceDB) -> UUID:
    tenant_id = uuid4()
    await db.admin.execute(
        "INSERT INTO tenants (id, name, slug, is_active) VALUES (%(id)s, 'T', %(slug)s, true)",
        {"id": tenant_id, "slug": tenant_id.hex},
    )
    return tenant_id


async def member(db: PersistenceDB, tenant_id: UUID, *, active: bool = True) -> UserRecord:
    identity_id, user_id = uuid4(), uuid4()
    await db.admin.execute(
        "INSERT INTO identities (id, email, is_active, is_platform_admin) VALUES (%(id)s, %(email)s, true, false)",
        {"id": identity_id, "email": f"{identity_id.hex}@example.test"},
    )
    await db.admin.execute(
        "INSERT INTO users (id, identity_id, tenant_id, display_name, role, is_active) "
        "VALUES (%(id)s, %(identity)s, %(tenant)s, 'Alice', 'member', %(active)s)",
        {"id": user_id, "identity": identity_id, "tenant": tenant_id, "active": active},
    )
    return UserRecord(id=user_id, identity_id=identity_id, tenant_id=tenant_id, display_name="Alice")


async def enqueue(db: PersistenceDB, count: int = 1) -> list[BroadcastEmailRecord]:
    tenant_id, broadcast_id = await tenant(db), uuid4()
    _ = await outbox.enqueue_many(
        [
            NewBroadcastEmail(broadcast_id, tenant_id, uuid4(), f"recipient{index}@example.test", "Subject", "Body")
            for index in range(count)
        ]
    )
    rows = await db.admin.fetchall(
        "SELECT * FROM broadcast_emails WHERE broadcast_id = %(id)s ORDER BY id", {"id": broadcast_id}
    )
    return [BroadcastEmailRecord.from_row(row) for row in rows]
