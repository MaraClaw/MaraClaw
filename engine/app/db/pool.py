"""Async connection pool lifecycle for psycopg3."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings
from app.core.logging import logger
from app.db.errors import DbError
from app.db.types import configure_connection
from app.db.url import normalize_psycopg_conninfo

type EnginePool = AsyncConnectionPool[AsyncConnection[DictRow]]

type PoolConfigure = Callable[[AsyncConnection[DictRow]], Awaitable[None]]

_pool: EnginePool | None = None
_durable: tuple[EnginePool, EnginePool] | None = None


@dataclass(frozen=True, slots=True)
class SessionLimits:
    """Server-side session limits, in milliseconds, applied to every connection of both pools."""

    statement_timeout_ms: int
    idle_in_transaction_session_timeout_ms: int


def _pool_configure(limits: SessionLimits) -> PoolConfigure:
    """Build the one ``configure`` hook shared by the main and durable pools.

    The hook commits, so psycopg_pool never sees the connection left in a transaction and discards it. The limits are
    session-level and survive the rollback the pool performs on every return, so reused checkouts keep them too.
    """

    async def configure(conn: AsyncConnection[DictRow]) -> None:
        configure_connection(conn)
        await conn.execute(
            "SELECT set_config('statement_timeout', %(statement)s, false), "
            "set_config('idle_in_transaction_session_timeout', %(idle)s, false)",
            {
                "statement": str(limits.statement_timeout_ms),
                "idle": str(limits.idle_in_transaction_session_timeout_ms),
            },
        )
        await conn.commit()

    return configure


def _session_limits() -> SessionLimits:
    settings = get_settings()
    return SessionLimits(
        statement_timeout_ms=settings.DATABASE_STATEMENT_TIMEOUT_MS,
        idle_in_transaction_session_timeout_ms=settings.DATABASE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS,
    )


def get_pool() -> EnginePool:
    """Return the process-global pool, or raise if not initialized."""
    if _pool is None:
        raise RuntimeError("psycopg pool is not initialized; call init_pool() during startup")
    return _pool


async def init_pool(
    *,
    conninfo: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    pool_timeout: float | None = None,
) -> EnginePool:
    """Create and open the global async connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    info = normalize_psycopg_conninfo(conninfo or settings.DATABASE_URL)
    pool_min = min_size if min_size is not None else settings.DATABASE_POOL_MIN_SIZE
    pool_max = max_size if max_size is not None else settings.DATABASE_POOL_MAX_SIZE
    wait_timeout = pool_timeout if pool_timeout is not None else settings.DATABASE_POOL_TIMEOUT

    if pool_min < 0:
        raise ValueError("DATABASE_POOL_MIN_SIZE must be >= 0")
    if pool_max < 1 or pool_max < pool_min:
        raise ValueError("DATABASE_POOL_MAX_SIZE must be >= 1 and >= min size")

    max_idle = float(getattr(settings, "DATABASE_POOL_MAX_IDLE", 600.0) or 600.0)
    max_lifetime = float(getattr(settings, "DATABASE_POOL_MAX_LIFETIME", 1800.0) or 1800.0)

    limits = _session_limits()

    # Do not subscript the constructor: tests replace AsyncConnectionPool with a
    # non-generic FakePool that raises TypeError on __class_getitem__.
    pool: EnginePool = AsyncConnectionPool(
        conninfo=info,
        min_size=pool_min,
        max_size=pool_max,
        timeout=wait_timeout,
        open=False,
        configure=_pool_configure(limits),
        check=_check_pooled_connection,
        kwargs={"autocommit": False, "row_factory": dict_row},
        name="maraclaw",
        max_idle=max_idle,
        max_lifetime=max_lifetime,
    )
    await pool.open()
    typed_pool = pool
    _pool = typed_pool
    logger.info(
        f"[db] psycopg pool opened min={pool_min} max={pool_max} timeout={wait_timeout}s "
        f"max_idle={max_idle}s max_lifetime={max_lifetime}s statement_timeout={limits.statement_timeout_ms}ms "
        f"idle_in_transaction_session_timeout={limits.idle_in_transaction_session_timeout_ms}ms"
    )
    return typed_pool


async def _check_pooled_connection(conn: AsyncConnection[DictRow]) -> None:
    """Reject already-closed sockets without a checkout-time ``SELECT 1``."""
    if conn.closed:
        raise OSError("psycopg connection is closed")


async def durable_pool() -> EnginePool:
    """Second pool on the process database for commits that must not join a request transaction.

    A request holding a connection of the main pool may wait here; nothing holding a connection of this pool waits
    on the main pool, so a saturated request pool cannot starve it. It is opened on first use from the main pool's
    own connection settings and follows that pool if it is replaced. Its size is ``DATABASE_DURABLE_POOL_MAX_SIZE``,
    independent of the main pool's ``max_size``.
    """
    global _durable
    try:
        source = get_pool()
    except RuntimeError as exc:
        raise DbError("database pool is not initialized", orig=exc) from exc
    held = _durable
    if held is None or held[0] is not source:
        pool: EnginePool = AsyncConnectionPool(
            conninfo=source.conninfo,
            min_size=0,
            max_size=get_settings().DATABASE_DURABLE_POOL_MAX_SIZE,
            timeout=source.timeout,
            open=False,
            configure=_pool_configure(_session_limits()),
            check=_check_pooled_connection,
            kwargs={"autocommit": False, "row_factory": dict_row},
            name="maraclaw-durable",
            max_idle=source.max_idle,
            max_lifetime=source.max_lifetime,
        )
        _durable = (source, pool)
        if held is not None:
            await held[1].close()
    else:
        pool = held[1]
    await pool.open()
    return pool


async def close_pool() -> None:
    """Close the global pool if open."""
    global _pool, _durable
    if _durable is not None:
        durable, _durable = _durable[1], None
        await durable.close()
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("[db] psycopg pool closed")


async def ping_pool() -> bool:
    """Return True when the pool can run ``SELECT 1``."""
    from app.core.json_types import int_from_row
    from app.db.connection import DbConnection

    pool = get_pool()
    async with pool.connection() as conn:
        db = DbConnection(conn)
        return int_from_row(await db.fetchval("SELECT 1")) == 1


def reset_pool_for_tests() -> None:
    """Clear the module-global pool reference without closing (tests only)."""
    global _pool, _durable
    _pool = None
    _durable = None
