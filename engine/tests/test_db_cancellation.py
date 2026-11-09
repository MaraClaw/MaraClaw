import asyncio

import pytest
from psycopg.errors import UniqueViolation

from app.db.connection import Params
from app.db.session import transaction
from test_db_connection import _FakeCursor, _FakeRawConnection, _wrap


async def test_repeated_native_cancellation_waits_for_query_drain_before_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _FakeRawConnection()
    db = _wrap(raw)
    cursor = _FakeCursor(raw)
    entered, draining, release, drained = (asyncio.Event() for _ in range(4))

    async def execute(query: str, params: Params = None) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            draining.set()
            await release.wait()
            drained.set()

    async def worker() -> None:
        async with transaction(db):
            await db.execute("SELECT blocked_query")

    monkeypatch.setattr(raw, "cursor", lambda: cursor)
    monkeypatch.setattr(cursor, "execute", execute)
    caller = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        caller.cancel("original cancellation")
        await asyncio.wait_for(draining.wait(), 1)
        for _ in range(3):
            caller.cancel("later cancellation")
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
        assert raw.rollbacks == 0
        assert not caller.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await caller

    assert drained.is_set()
    assert raw.rollbacks == 1
    assert raw.commits == 0
    assert caught.value.args == ("original cancellation",)


async def test_owner_cancellation_wins_over_completed_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _FakeRawConnection()
    db = _wrap(raw)
    cursor = _FakeCursor(raw)
    entered, release, failed = (asyncio.Event() for _ in range(3))

    async def execute(query: str, params: Params = None) -> None:
        entered.set()
        await release.wait()
        caller.cancel("owner cancellation")
        failed.set()
        raise UniqueViolation("query failed as owner was cancelled")

    async def worker() -> None:
        async with transaction(db):
            await db.execute("INSERT duplicate")

    monkeypatch.setattr(raw, "cursor", lambda: cursor)
    monkeypatch.setattr(cursor, "execute", execute)
    caller = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError, match="owner cancellation"):
            await caller

    assert failed.is_set()
    assert raw.rollbacks == 1
    assert raw.commits == 0
