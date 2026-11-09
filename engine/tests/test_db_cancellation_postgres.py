"""Real psycopg cancellation under AnyIO's repeated cancellation delivery."""

import os
from collections.abc import AsyncIterator
from typing import Literal

import anyio
import pytest
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow, dict_row

from app.db.connection import DbConnection
from app.db.errors import UniqueViolationError
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
