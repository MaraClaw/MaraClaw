"""Real psycopg cancellation under AnyIO's repeated cancellation delivery."""

import os
from collections.abc import AsyncIterator
from typing import Literal

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.errors import LockNotAvailable, UniqueViolation
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.db import pool as pool_module
from app.db.connection import DbConnection
from app.db.errors import DbOverloadError, LockNotAvailableError, LockWaitTimeoutError, UniqueViolationError
from app.db.session import get_connection, run_after_commit, transaction


@pytest.fixture
async def connections() -> AsyncIterator[tuple[DbConnection, AsyncConnection[DictRow]]]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    async with (
        await AsyncConnection[DictRow].connect(conninfo, row_factory=dict_row) as raw,
        await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as observer,
    ):
        await raw.execute("CREATE TEMP TABLE cancellation_probe (value integer PRIMARY KEY)")
        await raw.commit()
        try:
            yield DbConnection(raw), observer
        finally:
            # A broken cancellation may leave ACTIVE protocol state; closing is always safe.
            await raw.close()


@pytest.mark.parametrize("method", ["execute", "executemany", "fetchone", "fetchall", "fetchval"])
async def test_active_query_cancellation_rolls_back_and_connection_can_be_reused(
    connections: tuple[DbConnection, AsyncConnection[DictRow]],
    method: Literal["execute", "executemany", "fetchone", "fetchall", "fetchval"],
) -> None:
    db, observer = connections
    effects: list[str] = []
    cancelled = anyio.Event()
    query = "SELECT pg_sleep(60)"

    async def worker() -> None:
        try:
            async with transaction(db):
                run_after_commit(lambda: effects.append("commit"), lambda: effects.append("rollback"))
                await db.execute("INSERT INTO cancellation_probe VALUES (1)")
                if method == "executemany":
                    await db.executemany(query, [None])
                else:
                    await getattr(db, method)(query)
                pytest.fail("cancelled query returned normally")
        except anyio.get_cancelled_exc_class():
            cancelled.set()
            raise

    # Observe server-side execution, rather than guessing when the query has started.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker)
            while True:
                cursor = await observer.execute(
                    "SELECT wait_event FROM pg_stat_activity WHERE pid = %(pid)s",
                    {"pid": db.raw.info.backend_pid},
                )
                row = await cursor.fetchone()
                if row is not None and row["wait_event"] == "PgSleep":
                    break
            tasks.cancel_scope.cancel()

    assert cancelled.is_set()
    assert effects == ["rollback"]
    assert db.raw.info.transaction_status == TransactionStatus.IDLE
    assert get_connection() is None
    async with transaction(db):
        assert await db.fetchval("SELECT count(*) FROM cancellation_probe") == 0
        await db.execute("INSERT INTO cancellation_probe VALUES (2)")
    assert await db.fetchval("SELECT value FROM cancellation_probe") == 2


async def test_normal_commit_keeps_results_and_completion_callbacks(
    connections: tuple[DbConnection, AsyncConnection[DictRow]],
) -> None:
    db, _observer = connections
    effects: list[str] = []
    async with transaction(db):
        run_after_commit(lambda: effects.append("commit"), lambda: effects.append("rollback"))
        await db.executemany("INSERT INTO cancellation_probe VALUES (%(value)s)", [{"value": 1}, {"value": 2}])
        assert await db.fetchone("SELECT min(value) AS value FROM cancellation_probe") == {"value": 1}
        assert await db.fetchall("SELECT value FROM cancellation_probe ORDER BY value") == [{"value": 1}, {"value": 2}]
        assert effects == []

    assert effects == ["commit"]
    assert db.raw.info.transaction_status == TransactionStatus.IDLE
    assert await db.fetchval("SELECT count(*) FROM cancellation_probe") == 2


async def test_sql_error_keeps_original_error_rolls_back_and_runs_rollback_callback(
    connections: tuple[DbConnection, AsyncConnection[DictRow]],
) -> None:
    db, _observer = connections
    effects: list[str] = []

    async def insert_duplicate() -> None:
        async with transaction(db):
            run_after_commit(lambda: effects.append("commit"), lambda: effects.append("rollback"))
            await db.execute("INSERT INTO cancellation_probe VALUES (1)")
            await db.execute("INSERT INTO cancellation_probe VALUES (1)")

    with pytest.raises(UniqueViolationError) as caught:
        await insert_duplicate()

    assert isinstance(caught.value.orig, UniqueViolation)
    assert caught.value.__cause__ is caught.value.orig
    assert effects == ["rollback"]
    assert db.raw.info.transaction_status == TransactionStatus.IDLE
    assert await db.fetchval("SELECT count(*) FROM cancellation_probe") == 0


async def test_request_lock_timeout_is_a_503_overload_while_nowait_keeps_its_busy_error(
    connections: tuple[DbConnection, AsyncConnection[DictRow]],
) -> None:
    # Given a row another session holds locked, and a request connection carrying the request lock_timeout.
    db, holder = connections
    await holder.execute("CREATE TABLE request_lock_probe (id integer PRIMARY KEY)")
    await holder.execute("INSERT INTO request_lock_probe VALUES (1)")
    try:
        async with holder.transaction():
            _ = await holder.execute("SELECT id FROM request_lock_probe FOR UPDATE")
            await pool_module.set_request_lock_timeout(db, 150)

            # When the request waits for that row, its wait expires as an overload that is still a lock error.
            with pytest.raises(LockWaitTimeoutError) as waited:
                _ = await db.fetchone("SELECT id FROM request_lock_probe FOR UPDATE")
            assert isinstance(waited.value, LockNotAvailableError)
            assert isinstance(waited.value, DbOverloadError)
            assert isinstance(waited.value.orig, LockNotAvailable)
            assert waited.value.orig.diag.source_function == "ProcessInterrupts"
            await db.rollback()

            # Then a NOWAIT caller on the same SQLSTATE still gets the plain busy error, not an overload.
            with pytest.raises(LockNotAvailableError) as busy:
                _ = await db.fetchone("SELECT id FROM request_lock_probe FOR UPDATE NOWAIT")
            assert not isinstance(busy.value, DbOverloadError)
            assert isinstance(busy.value.orig, LockNotAvailable)
            assert busy.value.orig.diag.source_function not in (None, "ProcessInterrupts")
            await db.rollback()
    finally:
        await holder.execute("DROP TABLE request_lock_probe")


async def test_a_55p03_raised_by_server_side_code_with_the_timeout_text_is_not_a_lock_timeout(
    connections: tuple[DbConnection, AsyncConnection[DictRow]],
) -> None:
    # Given a refusal with SQLSTATE 55P03 and the lock timeout's English message, raised by PL/pgSQL.
    db, _observer = connections
    raising = "DO $$ BEGIN RAISE EXCEPTION 'canceling statement due to lock timeout' USING ERRCODE = '55P03'; END $$"

    # When it reaches the application, its source function is not ProcessInterrupts.
    with pytest.raises(LockNotAvailableError) as raised:
        _ = await db.execute(raising)
    await db.rollback()

    # Then the message alone does not make it an overload.
    assert not isinstance(raised.value, DbOverloadError)
    assert isinstance(raised.value.orig, LockNotAvailable)
    assert raised.value.orig.diag.message_primary == "canceling statement due to lock timeout"
    assert raised.value.orig.diag.source_function not in (None, "ProcessInterrupts")


async def test_pool_reset_removes_the_request_lock_timeout_before_the_connection_is_reused() -> None:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    pool = AsyncConnectionPool[AsyncConnection[DictRow]](
        conninfo,
        min_size=1,
        max_size=1,
        open=False,
        reset=pool_module._reset_request_lock_timeout,
        kwargs={"autocommit": False, "row_factory": dict_row},
    )
    await pool.open()
    try:
        async with pool.connection() as raw:
            await pool_module.set_request_lock_timeout(DbConnection(raw), 4321)
            await raw.commit()
            assert await DbConnection(raw).fetchval("SHOW lock_timeout") == "4321ms"
            await raw.commit()
        async with pool.connection() as reused:
            assert await DbConnection(reused).fetchval("SHOW lock_timeout") == "0"
    finally:
        await pool.close()
