"""Hold a PostgreSQL advisory lock from a test, and wait for sessions queued behind it."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from anyio import fail_after
from anyio.lowlevel import checkpoint

from app.core.json_types import int_from_row
from app.db.connection import DbConnection

_WAITERS = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
    "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


@asynccontextmanager
async def held_advisory_lock(db: DbConnection, key: str) -> AsyncGenerator[None]:
    """Hold the lock that ``pg_advisory_xact_lock(hashtextextended(key, 0))`` queues on, for the session of ``db``."""
    await db.execute("SELECT pg_advisory_lock(hashtextextended(%(key)s, 0))", {"key": key})
    try:
        yield
    finally:
        await db.execute("SELECT pg_advisory_unlock(hashtextextended(%(key)s, 0))", {"key": key})


async def wait_for_advisory_waiters(db: DbConnection, count: int) -> None:
    """Return once ``count`` sessions of this database are blocked on an advisory lock."""
    with fail_after(10):
        while int_from_row(await db.fetchval(_WAITERS)) < count:
            await checkpoint()
