"""Request-scoped psycopg connection helpers.

- request-scoped connection via ContextVar
- commit on success, rollback on exception for top-level scopes
- nested ``transaction()`` joins the existing connection
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from app.db.connection import DbConnection
from app.db.pool import get_pool

_conn_ctx: ContextVar[DbConnection | None] = ContextVar("psycopg_conn_ctx", default=None)


def get_connection() -> DbConnection | None:
    """Return the context-bound connection, if any."""
    return _conn_ctx.get()


@asynccontextmanager
async def connection_ctx() -> AsyncIterator[DbConnection]:
    """Yield a connection, reusing the context connection when present."""
    existing = _conn_ctx.get()
    if existing is not None:
        yield existing
        return

    pool = get_pool()
    async with pool.connection() as raw:
        db = DbConnection(raw)
        token = _conn_ctx.set(db)
        from app.core.access_cache import begin_deferred_acl, end_deferred_acl, flush_deferred_acl
        from app.core.redis_cache import begin_deferred_versions, end_deferred_versions, flush_deferred_versions

        acl_token = begin_deferred_acl()
        ver_token = begin_deferred_versions()
        try:
            yield db
            await db.commit()
            await flush_deferred_acl()
            await flush_deferred_versions()
        except Exception:
            await db.rollback()
            raise
        finally:
            end_deferred_acl(acl_token)
            end_deferred_versions(ver_token)
            _conn_ctx.reset(token)


async def flush_request_transaction() -> None:
    """Commit the request-scoped connection so later HTTP GETs can see writes.

    ``bind_crud_connection`` stays open until after the response (and any
    FastAPI background tasks) finish. Create-then-navigate would 404 without
    this flush.
    """
    db = _conn_ctx.get()
    if db is None:
        return
    from app.core.access_cache import flush_deferred_acl
    from app.core.redis_cache import flush_deferred_versions

    await db.commit()
    await flush_deferred_acl()
    await flush_deferred_versions()


async def get_db() -> AsyncGenerator[DbConnection]:
    """FastAPI dependency: request-scoped connection with commit/rollback."""
    async with connection_ctx() as db:
        yield db


async def bind_crud_connection() -> AsyncIterator[None]:
    """Bind one pool connection for a short CRUD HTTP request.

    Attach via ``dependencies=[Depends(bind_crud_connection)]`` on routers that
    only do database work. Do **not** use on websocket, connector inbound,
    gateway, or any handler that calls the LLM - that would pin a pool slot
    across model I/O.

    When the process pool is unset (unit tests without ``init_pool``), this is
    a no-op so ASGI tests keep working.
    """
    try:
        _ = get_pool()
    except RuntimeError:
        yield
        return
    async with connection_ctx():
        yield


@asynccontextmanager
async def optional_connection_ctx() -> AsyncIterator[DbConnection | None]:
    """Join/open a connection when the pool exists; otherwise yield ``None``.

    Use around batched DAO work that unit tests invoke without ``init_pool``.
    """
    if _conn_ctx.get() is not None:
        async with connection_ctx() as db:
            yield db
        return
    try:
        _ = get_pool()
    except RuntimeError:
        yield None
        return
    async with connection_ctx() as db:
        yield db


@asynccontextmanager
async def transaction(db: DbConnection | None = None) -> AsyncIterator[DbConnection]:
    """Transactional boundary compatible with nested call sites.

    - If ``db`` is provided, bind it as the context connection and commit/rollback
      around the block (caller-owned connection).
    - If a context connection already exists, join it (no nested commit).
    - Otherwise open a new pooled connection and commit/rollback.
    """
    if db is not None:
        token = _conn_ctx.set(db)
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            _conn_ctx.reset(token)
        return

    existing = _conn_ctx.get()
    if existing is not None:
        yield existing
        return

    async with connection_ctx() as conn:
        yield conn
