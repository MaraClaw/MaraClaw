"""Request-scoped psycopg connection helpers.

- request-scoped connection via ContextVar
- commit on success, rollback on exception for top-level scopes
- nested ``transaction()`` joins the existing connection
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.core.logging import logger
from app.db.connection import DbConnection
from app.db.pool import get_pool

_conn_ctx: ContextVar[DbConnection | None] = ContextVar("psycopg_conn_ctx", default=None)


@dataclass
class _CompletionBatch:
    callbacks: list[tuple[Callable[[], None], Callable[[], None] | None]] = field(default_factory=list)
    committed: bool = False


_completion_ctx: ContextVar[_CompletionBatch | None] = ContextVar("db_completion_batch", default=None)


def _dispatch_completion(batch: _CompletionBatch) -> None:
    callbacks, batch.callbacks = batch.callbacks, []
    for on_commit, on_rollback in callbacks:
        callback = on_commit if batch.committed else on_rollback
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                logger.opt(exception=exc).error("Transaction completion callback failed")


def run_after_commit(on_commit: Callable[[], None], on_rollback: Callable[[], None] | None = None) -> None:
    """Run after the owning transaction commits, or immediately without a scope.

    Raw savepoints (``db.raw.transaction()``) are not callback-aware.
    """
    batch = _completion_ctx.get()
    if batch is None:
        _dispatch_completion(_CompletionBatch([(on_commit, on_rollback)], committed=True))
    else:
        batch.callbacks.append((on_commit, on_rollback))


async def _commit_and_flush(db: DbConnection) -> None:
    from app.core.access_cache import flush_deferred_acl
    from app.core.redis_cache import flush_deferred_versions

    await db.commit()
    # Detach in place so later registrations stay on the scope's batch.
    batch = _completion_ctx.get()
    committed = None
    if batch is not None:
        committed = _CompletionBatch(batch.callbacks, committed=True)
        batch.callbacks = []
    try:
        await flush_deferred_acl()
        await flush_deferred_versions()
    finally:
        if committed is not None:
            _dispatch_completion(committed)


@asynccontextmanager
async def _own_transaction(db: DbConnection) -> AsyncGenerator[DbConnection]:
    from app.core.access_cache import begin_deferred_acl, end_deferred_acl
    from app.core.redis_cache import begin_deferred_versions, end_deferred_versions

    token = _conn_ctx.set(db)
    completion_token = _completion_ctx.set(_CompletionBatch())
    acl_token = begin_deferred_acl()
    ver_token = begin_deferred_versions()
    try:
        yield db
        await _commit_and_flush(db)
    except Exception:
        await db.rollback()
        raise
    finally:
        batch = _completion_ctx.get()
        if batch is not None:
            _dispatch_completion(batch)
        end_deferred_acl(acl_token)
        end_deferred_versions(ver_token)
        _completion_ctx.reset(completion_token)
        _conn_ctx.reset(token)


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
    async with pool.connection() as raw, _own_transaction(DbConnection(raw)) as db:
        yield db


async def flush_request_transaction() -> None:
    """Commit the request-scoped connection so later HTTP GETs can see writes.

    ``bind_crud_connection`` stays open until after the response (and any
    FastAPI background tasks) finish. Create-then-navigate would 404 without
    this flush.
    """
    db = _conn_ctx.get()
    if db is None:
        return
    await _commit_and_flush(db)


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
        async with _own_transaction(db):
            yield db
        return

    existing = _conn_ctx.get()
    if existing is not None:
        yield existing
        return

    async with connection_ctx() as conn:
        yield conn
