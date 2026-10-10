from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from tests.test_redis_cache import FakeRedis

from app.api import auth
from app.core import redis_cache, row_memo, session_cache, tenant_cache
from app.core.security import create_access_token
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.session import bind_crud_connection, connection_ctx
from app.records.user import UserRecord
from tenant_deletion_pg_fixtures import DeletionPostgres

pytest_plugins = ("tenant_deletion_pg_fixtures",)
pytestmark = pytest.mark.parametrize("deletion_pg", ["warm"], indirect=True)


class RollbackRequestedError(Exception):
    pass


@pytest.fixture
def wire_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRedis]:
    fake = FakeRedis()

    async def client() -> FakeRedis:
        return fake

    monkeypatch.setattr(redis_cache, "_client", client)
    redis_cache.reset_circuit()
    row_memo.clear_row_memo()
    yield fake
    row_memo.clear_row_memo()
    redis_cache.reset_circuit()


@pytest.mark.parametrize(("field", "value"), [("display_name", "Renamed"), ("username", "renamed")])
async def test_patch_returns_new_value_when_session_snapshot_is_warm(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis, field: str, value: str
) -> None:
    # Given: SQL-loaded serialization, not a mocked cache miss or auth dependency.
    assert await user_dao.get_with_identity(deletion_pg.actor.id) is not None
    key = redis_cache.cache_key("sess", "v1", deletion_pg.actor.id)
    assert key in wire_cache.store
    row_memo.clear_row_memo()
    app = FastAPI()
    app.include_router(auth.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    token = create_access_token(str(deletion_pg.actor.id), "org_admin")
    # When
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/api/auth/me", json={field: value}, headers={"Authorization": f"Bearer {token}"})
    # Then
    assert response.status_code == 200
    assert response.json()[field] == value
    query, row_id = (
        ("SELECT display_name FROM users WHERE id = %s", deletion_pg.actor.id)
        if field == "display_name"
        else ("SELECT username FROM identities WHERE id = %s", deletion_pg.actor.identity_id)
    )
    assert await deletion_pg.rows(query, (row_id,)) == [{field: value}]
    assert key not in wire_cache.store


async def test_get_returns_absent_when_warm_tenant_deleted_in_owning_transaction(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis
) -> None:
    # Given: a tenant without dependent rows, so this exercises the normal DAO delete.
    tenant_id = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO tenants (id, name, slug) VALUES (%s, 'Disposable', %s)", (tenant_id, str(tenant_id))
        )
    assert await tenant_dao.get(tenant_id) is not None
    key = redis_cache.cache_key("tenant", "v1", tenant_id)
    before = dict(wire_cache.store)
    row_memo.clear_row_memo()
    # When
    async with connection_ctx():
        await tenant_dao.delete(id=tenant_id)
        result = await tenant_dao.get(tenant_id)
        # Then: no premature deletion/publication while SQL is still uncommitted.
        assert result is None
        assert wire_cache.store == before
    assert key not in wire_cache.store


@pytest.mark.parametrize("kind", ["user", "identity", "tenant"])
async def test_rollback_keeps_sql_and_warm_redis_unchanged_when_dirty_snapshot_is_populated(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis, kind: str
) -> None:
    # Given
    user = await user_dao.get_with_identity(deletion_pg.actor.id)
    tenant = await tenant_dao.get(deletion_pg.tenant_id)
    assert user is not None
    assert user.identity is not None
    assert tenant is not None
    before_sql = await deletion_pg.snapshot()
    before_cache, before_expiry = dict(wire_cache.store), dict(wire_cache.expires)
    row_memo.clear_row_memo()
    # When
    try:
        async with connection_ctx():
            if kind == "tenant":
                updated_tenant = await tenant_dao.update(db_obj=tenant, obj_in={"name": "Uncommitted"})
                await tenant_cache.set_cached_tenant(updated_tenant)
                assert await tenant_dao.get(tenant.id) is updated_tenant
            else:
                if kind == "identity":
                    await identity_dao.update(db_obj=user.identity, obj_in={"username": "uncommitted"})
                else:
                    await user_dao.update(db_obj=user, obj_in={"display_name": "Uncommitted"})
                updated_user = await user_dao.get_with_identity(user.id, fresh=True)
                assert updated_user is not None
                await session_cache.set_cached_user(updated_user)
                assert await session_cache.get_cached_user(user.id) is updated_user
            assert wire_cache.store == before_cache
            raise RollbackRequestedError
    except RollbackRequestedError:
        pass
    else:
        pytest.fail("Owning transaction did not raise")
    # Then
    assert await deletion_pg.snapshot() == before_sql
    assert wire_cache.store == before_cache
    assert wire_cache.expires == before_expiry


@pytest.mark.parametrize("enumeration", ["empty", "fails"])
async def test_identity_version_alone_bypasses_warm_snapshot_when_members_are_unavailable(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch, enumeration: str
) -> None:
    # Given
    user = await user_dao.get_with_identity(deletion_pg.actor.id)
    assert user is not None
    assert user.identity_id is not None
    before = dict(wire_cache.store)
    row_memo.clear_row_memo()

    async def members(_identity_id: UUID) -> list[UserRecord]:
        if enumeration == "fails":
            raise RuntimeError("enumeration unavailable")
        return []

    monkeypatch.setattr(user_dao, "get_by_identity_id", members)
    # When
    try:
        async with connection_ctx() as db:
            await db.execute("UPDATE identities SET username = 'uncommitted' WHERE id = %s", (user.identity_id,))
            await session_cache.bump_identity_session(user.identity_id)
            loaded = await user_dao.get_with_identity(user.id)
            # Then
            assert loaded is not None
            assert loaded.username == "uncommitted"
            assert wire_cache.store == before
            raise RollbackRequestedError
    except RollbackRequestedError:
        pass
    else:
        pytest.fail("Owning transaction did not raise")
    assert wire_cache.store == before
