"""Async connection pool lifecycle for psycopg3."""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings
from app.core.logging import logger
from app.db.types import configure_connection
from app.db.url import normalize_psycopg_conninfo

_pool: AsyncConnectionPool[Any] | None = None


def get_pool() -> AsyncConnectionPool[Any]:
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
) -> AsyncConnectionPool[Any]:
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

    async def _configure(conn: AsyncConnection[Any]) -> None:
        configure_connection(conn)

    pool = AsyncConnectionPool(
        conninfo=info,
        min_size=pool_min,
        max_size=pool_max,
        timeout=wait_timeout,
        open=False,
        configure=_configure,
        check=_check_pooled_connection,
        kwargs={"autocommit": False},
        name="maraclaw",
        max_idle=max_idle,
        max_lifetime=max_lifetime,
    )
    await pool.open()
    _pool = pool
    logger.info(
        f"[db] psycopg pool opened min={pool_min} max={pool_max} timeout={wait_timeout}s "
        + f"max_idle={max_idle}s max_lifetime={max_lifetime}s"
    )
    return pool


async def _check_pooled_connection(conn: AsyncConnection[Any]) -> None:
    """Reject already-closed sockets without a checkout-time ``SELECT 1``."""
    if getattr(conn, "closed", False):
        raise OSError("psycopg connection is closed")


async def close_pool() -> None:
    """Close the global pool if open."""
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("[db] psycopg pool closed")


async def ping_pool() -> bool:
    """Return True when the pool can run ``SELECT 1``."""
    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 AS ok")
        row = await cur.fetchone()
        return bool(row and (row.get("ok") == 1 if isinstance(row, dict) else row[0] == 1))


def reset_pool_for_tests() -> None:
    """Clear the module-global pool reference without closing (tests only)."""
    global _pool
    _pool = None
