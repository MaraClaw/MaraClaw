from uuid import uuid4

import anyio
import pytest

import test_skill_refresh_postgres
from app.dao import tenant_dao
from app.dao.skill_dao import skill_dao
from app.dao.tenant_deletion_sql import TENANT_DELETE_CLEANUP_SQL
from app.db.errors import LockNotAvailableError
from app.db.session import connection_ctx
from app.services import skill_refresh

db = test_skill_refresh_postgres.db
registry = test_skill_refresh_postgres.registry


async def test_tenant_cleanup_locks_skill_parent_before_children(db, registry, monkeypatch):
    actor, before = registry
    tenant_id = uuid4()
    await db.execute(
        "INSERT INTO tenants (id,name,slug) VALUES (%(id)s,'Refresh tenant','refresh-tenant')", {"id": tenant_id}
    )
    await skill_dao.update(db_obj=before, obj_in={"tenant_id": tenant_id})
    locked, release = anyio.Event(), anyio.Event()
    publish = skill_dao.replace_imported_package
    first_cleanup = next(sql for sql in TENANT_DELETE_CLEANUP_SQL if "FROM skill" in sql)

    async def paused(*args):
        locked.set()
        await release.wait()
        return await publish(*args)

    async def refresh():
        await skill_refresh.refresh_skill(before.id, test_skill_refresh_postgres.refresh_request(before), actor)

    async def cleanup_entry():
        async with connection_ctx() as connection:
            await connection.execute("SET LOCAL lock_timeout = '200ms'")
            await connection.execute(first_cleanup, {"tid": tenant_id})

    async def cleanup():
        async with connection_ctx() as connection:
            await connection.execute("SET LOCAL lock_timeout = '200ms'")
            await tenant_dao.delete_cascade(tenant_id)

    monkeypatch.setattr(skill_dao, "replace_imported_package", paused)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(refresh)
            await locked.wait()
            with pytest.raises(LockNotAvailableError):
                await cleanup_entry()
            with pytest.raises(LockNotAvailableError):
                await cleanup()
            release.set()
    await tenant_dao.delete_cascade(tenant_id)
    assert await skill_dao.get_for_tenant_scope(before.id, tenant_id=None) is None
