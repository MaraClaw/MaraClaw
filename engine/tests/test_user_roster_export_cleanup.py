from collections.abc import Mapping, Sequence
from unittest.mock import AsyncMock
from uuid import UUID

import anyio
import pytest
from psycopg.pq import TransactionStatus

import user_roster_export_fixtures as fixtures
from app.dao.user_dao import UserPagePosition, user_dao
from app.db.connection import DbConnection, Params
from app.db.errors import DbError
from app.db.session import connection_ctx, get_connection
from app.records.user import UserRecord
from app.services import admin_audit
from app.services.user_roster_export import export_user_roster
from user_roster_export_fixtures import TENANT, RosterDatabase

roster_db = fixtures.roster_db


async def test_sets_snapshot_first_and_restores_request_when_generation_succeeds(
    roster_db: RosterDatabase, monkeypatch: pytest.MonkeyPatch
):
    execute = DbConnection.execute
    statements: list[tuple[DbConnection, str]] = []

    async def record(db: DbConnection, query: str, params: Params = None) -> None:
        statements.append((db, query))
        await execute(db, query, params)

    monkeypatch.setattr(DbConnection, "execute", record)
    async with connection_ctx() as outer:
        await outer.fetchval("SELECT 1")
        csv = await export_user_roster(TENANT, "", roster_db.actor)
        assert get_connection() is outer
        assert "Admin" in csv
        snapshot, first = statements[0]
        assert snapshot is not outer
        assert first == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        assert all(db is outer for db, _query in statements[1:])
        assert await outer.fetchval("SHOW transaction_read_only") == "off"
        assert await outer.fetchval("SELECT COUNT(*) FROM admin_audit_logs") == 1
    assert get_connection() is None
    async with roster_db.pool.connection() as raw:
        assert raw.info.transaction_status == TransactionStatus.IDLE
        assert await DbConnection(raw).fetchval("SHOW transaction_read_only") == "off"
        assert await DbConnection(raw).fetchval("SHOW transaction_isolation") == "read committed"


async def test_best_effort_audit_failure_keeps_generated_csv_and_request_transaction_usable(
    roster_db: RosterDatabase, monkeypatch: pytest.MonkeyPatch
):
    async def fail_audit(*, obj_in: Mapping[str, object]) -> None:
        db = get_connection()
        assert db is not None
        assert await db.fetchval("SHOW transaction_read_only") == "off"
        await db.execute("SELECT 1 / 0")

    monkeypatch.setattr(admin_audit.admin_audit_log_dao, "create", fail_audit)
    async with connection_ctx() as outer:
        result = await export_user_roster(TENANT, "", roster_db.actor)
        assert "Admin" in result
        await outer.execute("UPDATE users SET title='still writable'")
    assert await roster_db.writer.fetchval("SELECT title FROM users") == "still writable"
    assert await roster_db.writer.fetchval("SELECT COUNT(*) FROM admin_audit_logs") == 0


async def test_releases_snapshot_without_audit_when_roster_query_fails(
    roster_db: RosterDatabase, monkeypatch: pytest.MonkeyPatch
):
    audit = AsyncMock()
    monkeypatch.setattr("app.services.user_roster_export.write_admin_audit", audit)
    original = user_dao.list_for_tenant_page
    connections: list[DbConnection] = []

    async def fail(
        tenant_id: UUID, *, search: str = "", after: UserPagePosition | None = None, limit: int = 50
    ) -> Sequence[UserRecord]:
        db = get_connection()
        assert db is not None
        connections.append(db)
        await original(tenant_id, search=search, after=after, limit=limit)
        await db.execute("UPDATE users SET title='forbidden'")
        raise AssertionError("read-only export accepted a write")

    monkeypatch.setattr(user_dao, "list_for_tenant_page", fail)
    async with connection_ctx() as outer:
        with pytest.raises(DbError, match="read-only transaction"):
            await export_user_roster(TENANT, "", roster_db.actor)
        assert get_connection() is outer
        assert await outer.fetchval("SELECT 1") == 1
    assert connections[0].raw.info.transaction_status == TransactionStatus.IDLE
    assert roster_db.pool.get_stats()["pool_available"] == roster_db.pool.get_stats()["pool_size"]
    audit.assert_not_awaited()


async def test_releases_snapshot_and_restores_context_when_export_task_is_cancelled(
    roster_db: RosterDatabase, monkeypatch: pytest.MonkeyPatch
):
    started = anyio.Event()
    finished = anyio.Event()
    connections: list[DbConnection] = []
    audit = AsyncMock()
    monkeypatch.setattr("app.services.user_roster_export.write_admin_audit", audit)
    original = user_dao.list_for_tenant_page

    async def pause(
        tenant_id: UUID, *, search: str = "", after: UserPagePosition | None = None, limit: int = 50
    ) -> Sequence[UserRecord]:
        db = get_connection()
        assert db is not None
        connections.append(db)
        rows = await original(tenant_id, search=search, after=after, limit=limit)
        started.set()
        await anyio.sleep_forever()
        return rows

    monkeypatch.setattr(user_dao, "list_for_tenant_page", pause)
    async with connection_ctx() as outer:
        scope = anyio.CancelScope()

        async def worker() -> None:
            try:
                with scope:
                    try:
                        await export_user_roster(TENANT, "", roster_db.actor)
                    except anyio.get_cancelled_exc_class():
                        assert get_connection() is outer
                        raise
            finally:
                finished.set()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(worker)
                await started.wait()
                scope.cancel()
                await finished.wait()
        assert get_connection() is outer
        assert await outer.fetchval("SELECT 1") == 1
    raw = connections[0].raw
    assert raw.closed or raw.info.transaction_status == TransactionStatus.IDLE
    async with roster_db.pool.connection() as replacement:
        assert await DbConnection(replacement).fetchval("SHOW transaction_read_only") == "off"
        assert await DbConnection(replacement).fetchval("SHOW transaction_isolation") == "read committed"
    assert roster_db.pool.get_stats()["requests_waiting"] == 0
    audit.assert_not_awaited()
