"""HTTP mapping of database overload: 503 + Retry-After, everything else keeps the catch-all 500."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
import anyio.lowlevel
import httpx
import pytest
from fastapi import Depends
from psycopg import AsyncConnection
from psycopg.errors import LockNotAvailable, QueryCanceled, UniqueViolation
from psycopg.pq import DiagnosticField
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout, TooManyRequests

from app import main as main_module
from app.db import pool as pool_module
from app.db.errors import (
    DbError,
    LockNotAvailableError,
    LockWaitTimeoutError,
    PoolCheckoutTimeoutError,
    PoolQueueFullError,
    QueryCanceledError,
    UniqueViolationError,
)
from app.db.session import bind_crud_connection

_PATH = "/_test/db-overload"


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[httpx.AsyncClient, list[BaseException]]]:
    reported: list[BaseException] = []

    def record(exc: BaseException, **_kwargs: object) -> None:
        reported.append(exc)

    monkeypatch.setattr(main_module, "report_exception", record)
    transport = httpx.ASGITransport(app=main_module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, reported


def _raising(exc: BaseException) -> Callable[[], Awaitable[None]]:
    async def endpoint() -> None:
        raise exc

    return endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        PoolTimeout("couldn't get a connection after 30.00 sec"),
        QueryCanceled("canceling statement due to statement timeout"),
        PoolCheckoutTimeoutError(orig=PoolTimeout("timeout")),
        QueryCanceledError(orig=QueryCanceled("canceled")),
        DbError("wrapped", orig=QueryCanceled("canceled")),
        DbError("wrapped", orig=PoolTimeout("timeout")),
        TooManyRequests("too many requests"),
        PoolQueueFullError(orig=TooManyRequests("too many requests")),
        LockWaitTimeoutError(),
        LockNotAvailable("lock", info={DiagnosticField.MESSAGE_PRIMARY: b"canceling statement due to lock timeout"}),
    ],
    ids=[
        "raw-pool-timeout",
        "raw-query-canceled",
        "typed-pool",
        "typed-cancel",
        "dberror-cancel",
        "dberror-pool",
        "raw-queue-full",
        "typed-queue-full",
        "typed-lock-wait",
        "raw-lock-wait",
    ],
)
async def test_overload_answers_503_with_retry_after(
    client: tuple[httpx.AsyncClient, list[BaseException]], exc: BaseException
) -> None:
    http, reported = client
    main_module.app.add_api_route(_PATH, _raising(exc))
    try:
        response = await http.get(_PATH)
    finally:
        main_module.app.router.routes.pop()

    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(QueryCanceledError.retry_after_seconds)
    assert isinstance(response.json()["detail"], str)
    assert reported == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        DbError("plain"),
        UniqueViolationError(orig=UniqueViolation("duplicate")),
        RuntimeError("boom"),
        LockNotAvailableError(orig=LockNotAvailable("busy")),
        LockNotAvailable("could not obtain lock on row"),
    ],
    ids=["db-error", "unique-violation", "runtime-error", "nowait-busy", "raw-nowait-busy"],
)
async def test_other_exceptions_keep_report_and_500(
    client: tuple[httpx.AsyncClient, list[BaseException]], exc: BaseException
) -> None:
    http, reported = client
    main_module.app.add_api_route(_PATH, _raising(exc))
    try:
        response = await http.get(_PATH)
    finally:
        main_module.app.router.routes.pop()

    assert response.status_code == 500
    assert "Retry-After" not in response.headers
    assert reported == [exc]


@pytest.fixture
async def one_slot_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncConnectionPool[AsyncConnection[DictRow]]]:
    conninfo = os.environ.get("MARACLAW_TEST_POSTGRES_DSN", "")
    if not conninfo:
        pytest.skip("MARACLAW_TEST_POSTGRES_DSN is required for the real pool queue")
    pool = AsyncConnectionPool[AsyncConnection[DictRow]](
        conninfo,
        min_size=1,
        max_size=1,
        max_waiting=1,
        timeout=30,
        open=False,
        reset=pool_module._reset_request_lock_timeout,
        kwargs={"autocommit": False, "row_factory": dict_row},
    )
    await pool.open()
    monkeypatch.setattr(pool_module, "_pool", pool)
    try:
        yield pool
    finally:
        monkeypatch.setattr(pool_module, "_pool", None)
        await pool.close()


@pytest.mark.asyncio
async def test_full_pool_and_queue_fail_the_next_request_fast(
    client: tuple[httpx.AsyncClient, list[BaseException]],
    one_slot_pool: AsyncConnectionPool[AsyncConnection[DictRow]],
) -> None:
    # Given a pool of one connection with one queued checkout behind a holder that keeps the only slot.
    http, reported = client
    holder_in, queued_in, release = anyio.Event(), anyio.Event(), anyio.Event()

    async def hold() -> None:
        async with one_slot_pool.connection():
            holder_in.set()
            await release.wait()

    async def queue() -> None:
        async with one_slot_pool.connection():
            queued_in.set()

    async def probe() -> dict[str, bool]:
        return {"ok": True}

    main_module.app.add_api_route(_PATH, probe, dependencies=[Depends(bind_crud_connection)])
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(hold)
            with anyio.fail_after(5):
                await holder_in.wait()
            group.start_soon(queue)
            with anyio.fail_after(5):
                while one_slot_pool.get_stats().get("requests_waiting", 0) < 1:
                    await anyio.lowlevel.checkpoint()
            # When another CRUD request needs a connection.
            with anyio.fail_after(5):
                response = await http.get(_PATH)
            # Then it is refused at once with a retry hint, and the queued checkout is untouched.
            assert response.status_code == 503
            assert response.headers["Retry-After"] == str(PoolQueueFullError.retry_after_seconds)
            assert not queued_in.is_set()
            release.set()
            with anyio.fail_after(5):
                await queued_in.wait()
    finally:
        main_module.app.router.routes.pop()
    assert reported == []
