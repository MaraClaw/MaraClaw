from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.asyncio.connection import Connection
from tests.test_redis_cache import FakeRedis

from app.api import auth
from app.core import redis_cache, row_memo, session_cache, tenant_cache
from app.core.security import create_access_token, load_user_from_access_token
from app.dao.identity_dao import identity_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db import session
from app.db.connection import DbConnection, Params, Row
from app.db.session import bind_crud_connection, connection_ctx
from app.records.user import UserRecord
from app.services import cache_invalidation_worker as worker

if TYPE_CHECKING:
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
    session_cache._identity_hints.clear()
    yield fake
    row_memo.clear_row_memo()
    session_cache._identity_hints.clear()
    redis_cache.reset_circuit()


@pytest.mark.parametrize(("field", "value"), [("display_name", "Renamed"), ("email", "renamed@example.com")])
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
    token = await create_access_token(str(deletion_pg.actor.id), "org_admin")
    # When
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/api/auth/me", json={field: value}, headers={"Authorization": f"Bearer {token}"})
    # Then
    assert response.status_code == 200
    assert response.json()[field] == value
    query, row_id = (
        ("SELECT display_name FROM users WHERE id = %s", deletion_pg.actor.id)
        if field == "display_name"
        else ("SELECT email FROM identities WHERE id = %s", deletion_pg.actor.identity_id)
    )
    assert await deletion_pg.rows(query, (row_id,)) == [{field: value}]
    assert key not in wire_cache.store


@pytest.mark.parametrize("moved", ["membership", "identity"])
async def test_a_moved_generation_rejects_the_next_request_though_the_session_snapshot_is_warm(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis, moved: str
) -> None:
    # Given a bearer for the actor whose session snapshot is warm and answers in one Redis call.
    actor = deletion_pg.actor
    token = await create_access_token(str(actor.id), "org_admin")
    row_memo.clear_row_memo()
    assert (await load_user_from_access_token(token)).id == actor.id
    key = redis_cache.cache_key("sess", "v1", actor.id)
    assert key in wire_cache.store
    row_memo.clear_row_memo()
    wire_cache.commands.clear()
    assert (await load_user_from_access_token(token)).id == actor.id
    assert wire_cache.commands == ["pipeline"]

    # When SQL moves one generation without any cache invalidation.
    update, row_id = (
        ("UPDATE users SET membership_generation = membership_generation + 1 WHERE id = %s RETURNING id", actor.id)
        if moved == "membership"
        else (
            "UPDATE identities SET auth_generation = auth_generation + 1 WHERE id = %s RETURNING id",
            actor.identity_id,
        )
    )
    assert await deletion_pg.rows(update, (row_id,)) == [{"id": row_id}]

    # Then the next request is refused even though the snapshot still hits in one Redis call.
    row_memo.clear_row_memo()
    wire_cache.commands.clear()
    with pytest.raises(HTTPException) as refused:
        await load_user_from_access_token(token)
    assert refused.value.status_code == 401
    assert key in wire_cache.store
    assert wire_cache.commands == ["pipeline"]


async def test_one_statement_returns_both_generations_and_only_for_the_bound_identity(
    deletion_pg: DeletionPostgres,
) -> None:
    # Given a membership with moved counters.
    actor = deletion_pg.actor
    assert actor.identity_id is not None
    await deletion_pg.rows(
        "UPDATE users SET membership_generation = 4 WHERE id = %s RETURNING id",
        (actor.id,),
    )
    await deletion_pg.rows("UPDATE identities SET auth_generation = 9 WHERE id = %s RETURNING id", (actor.identity_id,))

    # When both are read through the DAO, then they come from SQL as stored.
    assert await user_dao.get_generations(actor.id, actor.identity_id) == {
        "auth_generation": 9,
        "membership_generation": 4,
    }
    # And an identity that is not the membership's, or a membership that is gone, reads as nothing.
    assert await user_dao.get_generations(actor.id, uuid4()) is None
    assert await user_dao.get_generations(uuid4(), actor.identity_id) is None


async def test_get_returns_absent_when_warm_tenant_deleted_in_owning_transaction(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis
) -> None:
    # Given: a tenant without dependent rows, so this exercises the normal DAO delete.
    tenant_id = uuid4()
    async with connection_ctx() as db:
        await db.execute(
            "INSERT INTO tenants (id, name, slug, tier) VALUES (%s, 'Disposable', %s, 'ultra')",
            (tenant_id, str(tenant_id)),
        )
    assert await tenant_dao.get(tenant_id) is not None
    key = redis_cache.cache_key("tenant", "v1", tenant_id)
    before = dict(wire_cache.store)
    row_memo.clear_row_memo()
    # When
    async with connection_ctx():
        await tenant_dao.delete(record_id=tenant_id)
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
                    await identity_dao.update(db_obj=user.identity, obj_in={"email": "uncommitted@example.com"})
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
            await db.execute(
                "UPDATE identities SET email = 'uncommitted@example.com' WHERE id = %s", (user.identity_id,)
            )
            await session_cache.bump_identity_session(user.identity_id)
            loaded = await user_dao.get_with_identity(user.id)
            # Then
            assert loaded is not None
            assert loaded.email == "uncommitted@example.com"
            assert wire_cache.store == before
            raise RollbackRequestedError
    except RollbackRequestedError:
        pass
    else:
        pytest.fail("Owning transaction did not raise")
    assert wire_cache.store == before


@pytest.mark.parametrize("rebind", [False, True])
async def test_identity_write_interleaving_refuses_unobserved_session_fill(
    deletion_pg: DeletionPostgres, wire_cache: FakeRedis, monkeypatch: pytest.MonkeyPatch, rebind: bool
) -> None:
    actor = deletion_pg.actor
    assert actor.identity is not None
    joined, resume = anyio.Event(), anyio.Event()
    original = DbConnection.fetchone
    results: list[UserRecord | None] = []
    paused = False

    async def fetchone(db: DbConnection, query: str, params: Params = None) -> Row | None:
        nonlocal paused
        target = not paused and "FROM users u LEFT JOIN identities" in query and params == {"user_id": actor.id}
        if target:
            paused = True
            if rebind:
                joined.set()
                await resume.wait()
        row = await original(db, query, params)
        if target and not rebind:
            joined.set()
            await resume.wait()
        return row

    async def reader() -> None:
        results.append(await user_dao.get_with_identity(actor.id))

    monkeypatch.setattr(DbConnection, "fetchone", fetchone)
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(reader)
            await joined.wait()
            async with connection_ctx() as db:
                if rebind:
                    identity_id = uuid4()
                    await db.execute(
                        "INSERT INTO identities (id, email, password_hash) VALUES (%(id)s, %(email)s, 'hash')",
                        {"id": identity_id, "email": f"{identity_id}@example.test"},
                    )
                    await db.execute(
                        "UPDATE users SET identity_id = %(identity)s WHERE id = %(actor)s",
                        {"identity": identity_id, "actor": actor.id},
                    )
                else:
                    await identity_dao.update(db_obj=actor.identity, obj_in={"must_change_password": True})
            resume.set()

    assert results[0] is not None
    assert redis_cache.cache_key("sess", "v1", actor.id) not in wire_cache.store
    row_memo.clear_row_memo()
    current = await user_dao.get_with_identity(actor.id)
    assert current is not None
    assert (current.identity_id != actor.identity_id) if rebind else current.must_change_password
    row_memo.clear_row_memo()
    cached = await user_dao.get_with_identity(actor.id)
    assert cached is not None
    assert cached.identity is not None
    assert cached.identity.password_hash is None
    assert cached.identity_id == current.identity_id
    assert cached.must_change_password == current.must_change_password
    fresh = await user_dao.get_with_identity(actor.id, fresh=True)
    assert fresh is not None
    assert fresh.identity is not None
    assert fresh.identity.password_hash == "hash"
    assert await user_dao.get_with_identity(actor.id) is cached


@pytest.mark.parametrize("kind", ["user", "tenant"])
async def test_recovered_generation_rejects_late_real_redis_snapshot_fill(
    deletion_pg: DeletionPostgres, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    url = os.environ.get("MARACLAW_TEST_REDIS_URL", "")
    if not url:
        pytest.skip("MARACLAW_TEST_REDIS_URL required for real stale-fill interleaving")
    async with Redis.from_url(url, decode_responses=True) as client:

        async def real_client() -> Redis:
            return client

        monkeypatch.setattr(redis_cache, "_client", real_client)
        monkeypatch.setattr(worker, "get_cache_redis", real_client)
        monkeypatch.setattr(session, "_flush_cache_invalidations", AsyncMock())
        row_memo.clear_row_memo()
        if kind == "user":
            user = await user_dao.get_with_identity(deletion_pg.actor.id)
            assert user is not None
            key = redis_cache.cache_key("sess", "v1", user.id)
            old_payload = await client.get(key)
            async with connection_ctx():
                await user_dao.update(db_obj=user, obj_in={"display_name": "Committed"})
        else:
            tenant = await tenant_dao.get(deletion_pg.tenant_id)
            assert tenant is not None
            key = redis_cache.cache_key("tenant", "v1", tenant.id)
            old_payload = await client.get(key)
            async with connection_ctx():
                await tenant_dao.update(db_obj=tenant, obj_in={"name": "Committed"})
        assert old_payload is not None
        assert await worker.replay_cache_invalidations() >= 2
        await client.set(key, old_payload, ex=60)
        row_memo.clear_row_memo()
        if kind == "user":
            current_user = await user_dao.get_with_identity(deletion_pg.actor.id)
            assert current_user is not None
            assert current_user.display_name == "Committed"
        else:
            current_tenant = await tenant_dao.get(deletion_pg.tenant_id)
            assert current_tenant is not None
            assert current_tenant.name == "Committed"


def _session_keys(actor: UserRecord) -> tuple[str, str, str]:
    assert actor.identity_id is not None
    return (
        redis_cache.cache_key("sess", "v1", actor.id),
        session_cache.user_version_key(actor.id),
        session_cache.identity_version_key(actor.identity_id),
    )


def _gets(*keys: str) -> bytes:
    """The bytes one pipeline of single-key GETs writes to the socket."""
    return b"".join(f"*2\r\n$3\r\nGET\r\n${len(key)}\r\n{key}\r\n".encode() for key in keys)


@pytest.fixture
async def real_cache(
    monkeypatch: pytest.MonkeyPatch, deletion_pg: DeletionPostgres
) -> AsyncIterator[tuple[Redis, list[bytes]]]:
    """A real Redis behind the cache helpers, and each socket write the client makes from here on."""
    url = os.environ.get("MARACLAW_TEST_REDIS_URL", "")
    if not url:
        pytest.skip("MARACLAW_TEST_REDIS_URL required for the real session read")
    async with Redis.from_url(url, decode_responses=True) as client:

        async def real_client() -> Redis:
            return client

        monkeypatch.setattr(redis_cache, "_client", real_client)
        _ = await client.ping()
        writes: list[bytes] = []
        send = Connection.send_packed_command

        async def recording(
            self: Connection, command: bytes | str | Iterable[bytes], check_health: bool = True
        ) -> None:
            if isinstance(command, str):
                command = command.encode()
            chunks = [command] if isinstance(command, bytes) else list(command)
            writes.append(b"".join(chunks))
            await send(self, chunks, check_health=check_health)

        monkeypatch.setattr(Connection, "send_packed_command", recording)
        redis_cache.reset_circuit()
        row_memo.clear_row_memo()
        session_cache._identity_hints.clear()
        yield client, writes
        _ = await client.delete(*_session_keys(deletion_pg.actor))
        row_memo.clear_row_memo()
        session_cache._identity_hints.clear()
        redis_cache.reset_circuit()


@pytest.mark.parametrize("stored", ["snapshot", "garbage"])
async def test_real_redis_serves_a_session_hit_as_one_write_naming_each_key(
    deletion_pg: DeletionPostgres, real_cache: tuple[Redis, list[bytes]], stored: str
) -> None:
    client, writes = real_cache
    actor = deletion_pg.actor
    keys = _session_keys(actor)
    # Given the snapshot filled through SQL, or garbage standing where the snapshot belongs.
    assert await user_dao.get_with_identity(actor.id) is not None
    if stored == "garbage":
        _ = await client.set(keys[0], "{not json", ex=60)
    row_memo.clear_row_memo()
    writes.clear()

    # When the next request loads the principal.
    loaded = await user_dao.get_with_identity(actor.id)

    # Then the first write is one pipeline of three GETs, which alone serves a stored snapshot.
    assert loaded is not None
    assert loaded.id == actor.id
    assert writes[0] == _gets(*keys)
    assert (len(writes) == 1) is (stored == "snapshot")


async def test_real_redis_serves_the_first_read_of_a_membership_in_two_writes(
    deletion_pg: DeletionPostgres, real_cache: tuple[Redis, list[bytes]]
) -> None:
    _, writes = real_cache
    actor = deletion_pg.actor
    sess_key, user_version_key, identity_version_key = _session_keys(actor)
    # Given a stored snapshot in a process that has not served this membership yet.
    assert await user_dao.get_with_identity(actor.id) is not None
    session_cache._identity_hints.clear()
    row_memo.clear_row_memo()
    writes.clear()

    # When the membership is read twice.
    assert await user_dao.get_with_identity(actor.id) is not None
    first = list(writes)
    row_memo.clear_row_memo()
    writes.clear()
    assert await user_dao.get_with_identity(actor.id) is not None

    # Then only the first read pays a second write, for the identity generation the snapshot names.
    assert first == [_gets(sess_key, user_version_key), _gets(identity_version_key)]
    assert writes == [_gets(sess_key, user_version_key, identity_version_key)]


@pytest.mark.parametrize("hinted", [True, False])
@pytest.mark.parametrize("moved", ["user", "identity"])
async def test_real_redis_generation_alone_refuses_the_stored_snapshot_whether_or_not_the_identity_is_hinted(
    deletion_pg: DeletionPostgres, real_cache: tuple[Redis, list[bytes]], moved: str, hinted: bool
) -> None:
    client, _ = real_cache
    actor = deletion_pg.actor
    sess_key, user_version_key, identity_version_key = _session_keys(actor)
    # Given a stored snapshot, read through the hint or by naming the identity from the snapshot.
    assert await user_dao.get_with_identity(actor.id) is not None
    if not hinted:
        session_cache._identity_hints.clear()
    row_memo.clear_row_memo()

    # When one generation rotates while the snapshot itself stays stored.
    await redis_cache.bump_version(user_version_key if moved == "user" else identity_version_key)

    # Then the generation alone refuses the snapshot.
    assert await client.exists(sess_key) == 1
    assert await session_cache.get_cached_user(actor.id) is None


async def test_real_redis_hint_naming_another_identity_is_a_miss_and_is_corrected_for_the_next_read(
    deletion_pg: DeletionPostgres, real_cache: tuple[Redis, list[bytes]]
) -> None:
    client, writes = real_cache
    actor = deletion_pg.actor
    keys = _session_keys(actor)
    # Given a stored snapshot, and a hint naming an identity whose generation equals the snapshot's.
    assert await user_dao.get_with_identity(actor.id) is not None
    generation = await client.get(keys[2])
    assert generation is not None
    wrong = uuid4()
    _ = await client.set(session_cache.identity_version_key(wrong), generation, ex=60)
    session_cache._identity_hints[actor.id] = wrong
    row_memo.clear_row_memo()

    # When the membership is read.
    assert await session_cache.get_cached_user(actor.id) is None

    # Then the wrong key's generation vouched for nothing, and the next read hits in one write with the right hint.
    assert session_cache._identity_hints[actor.id] == actor.identity_id
    writes.clear()
    assert await session_cache.get_cached_user(actor.id) is not None
    assert writes == [_gets(*keys)]


async def test_real_redis_session_read_that_fails_on_the_server_is_a_miss_after_one_attempt(
    deletion_pg: DeletionPostgres, real_cache: tuple[Redis, list[bytes]]
) -> None:
    client, writes = real_cache
    actor = deletion_pg.actor
    _, user_version_key, _ = _session_keys(actor)
    # Given a stored snapshot and a user generation key whose GET the server refuses (it holds a list).
    assert await user_dao.get_with_identity(actor.id) is not None
    _ = await client.delete(user_version_key)
    _ = await client.rpush(user_version_key, "not a generation")
    row_memo.clear_row_memo()
    writes.clear()

    # When the membership is read twice.
    first = await session_cache.get_cached_user(actor.id)
    second = await session_cache.get_cached_user(actor.id)

    # Then both fall open to SQL and only the first reached Redis.
    assert (first, second) == (None, None)
    assert len(writes) == 1
