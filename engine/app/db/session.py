"""Request-scoped psycopg connection helpers.

- request-scoped connection via ContextVar
- commit on success, rollback on exception for top-level scopes
- nested ``transaction()`` joins the existing connection
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from anyio import CancelScope
from psycopg.pq import TransactionStatus

from app.config import get_settings
from app.core.logging import logger
from app.db.connection import DbConnection
from app.db.pool import durable_pool, get_pool

_conn_ctx: ContextVar[DbConnection | None] = ContextVar("psycopg_conn_ctx", default=None)

_SHOW_STATEMENT_TIMEOUT_SQL = "SELECT current_setting('statement_timeout') AS value"
_SET_LOCAL_STATEMENT_TIMEOUT_SQL = "SELECT set_config('statement_timeout', %(value)s, true)"


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


async def _flush_cache_invalidations() -> None:
    """Publish both deferred invalidation families under one deadline; the commit is already durable."""
    from app.core.access_cache import flush_deferred_acl, has_pending_acl_writes
    from app.core.redis_cache import flush_deferred_versions, has_pending_snapshot_writes

    # Scheduling tasks yields to the loop, which an empty commit must not do before releasing its checkout.
    if not (has_pending_acl_writes() or has_pending_snapshot_writes()):
        return
    deadline = float(getattr(get_settings(), "REDIS_CACHE_WAIT_SECONDS", 0.2) or 0.2)
    try:
        async with asyncio.timeout(deadline):
            outcomes = await asyncio.gather(flush_deferred_acl(), flush_deferred_versions(), return_exceptions=True)
    except TimeoutError:
        logger.warning("Post-commit cache invalidation exceeded {}s and was abandoned", deadline)
        return
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


async def _commit_and_flush(db: DbConnection) -> None:
    from app.core.access_cache import pending_acl_invalidations
    from app.core.redis_cache import pending_snapshot_invalidations
    from app.dao.cache_invalidation_dao import cache_invalidation_dao

    # Detach in place so later registrations stay on the scope's batch.
    batch = _completion_ctx.get()
    pending = None
    if batch is not None:
        pending = _CompletionBatch(batch.callbacks)
        batch.callbacks = []
    try:
        await cache_invalidation_dao.capture(db, (*pending_acl_invalidations(), *pending_snapshot_invalidations()))
        await db.commit()
    except BaseException:
        try:
            with CancelScope(shield=True):
                await db.rollback()
        finally:
            if pending is not None:
                _dispatch_completion(pending)
        raise
    if pending is not None:
        pending.committed = True
    try:
        await _flush_cache_invalidations()
    finally:
        if pending is not None:
            _dispatch_completion(pending)


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
    except BaseException:
        # Explicit connections have no pool checkout to roll back cancelled work for them.
        with CancelScope(shield=True):
            await db.rollback()
        raise
    else:
        await _commit_and_flush(db)
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
async def connection_ctx() -> AsyncGenerator[DbConnection]:
    """Yield a connection, reusing the context connection when present."""
    existing = _conn_ctx.get()
    if existing is not None:
        yield existing
        return

    pool = get_pool()
    async with pool.connection() as raw, _own_transaction(DbConnection(raw)) as db:
        yield db


async def _set_statement_timeout(db: DbConnection, value: str) -> None:
    await db.execute(_SET_LOCAL_STATEMENT_TIMEOUT_SQL, {"value": value})


@asynccontextmanager
async def long_running_statement(db: DbConnection) -> AsyncGenerator[None]:
    """Lift ``statement_timeout`` for one scoped block of deliberately long work.

    The override is transaction-local (``set_config(..., is_local => true)``), so a leaked value can never outlive
    the enclosing transaction or reach the pool. On exit the value read on entry is restored, so later statements of
    a request-bound transaction keep the configured limit and nested scopes unwind one level at a time. When the
    block failed or the transaction is already aborted, restoring is best effort: the original exception always
    propagates and the owner's rollback (which also discards the local setting) is unchanged. Cancellation
    (``BaseException``) skips the restore for the same reason. Never touches ``lock_timeout``.
    """
    prior = str(await db.fetchval(_SHOW_STATEMENT_TIMEOUT_SQL))
    await _set_statement_timeout(db, "0")
    try:
        yield
    except Exception:
        if db.raw.info.transaction_status != TransactionStatus.INERROR:
            try:
                await _set_statement_timeout(db, prior)
            except Exception as exc:
                logger.opt(exception=exc).warning("Could not restore statement_timeout after a failed scope")
        raise
    if db.raw.info.transaction_status != TransactionStatus.INERROR:
        await _set_statement_timeout(db, prior)


@asynccontextmanager
async def durable_transaction() -> AsyncGenerator[DbConnection]:
    """Commit on a connection of the durable pool, never joining the enclosing request transaction.

    Use when a commit must be durable before the caller continues (a reservation ahead of an external write) and
    the caller's own transaction holds unrelated uncommitted work or one of the main pool's connections. DAO calls
    inside the block use this connection; the enclosing context connection is restored afterwards.
    """
    pool = await durable_pool()
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
async def optional_connection_ctx() -> AsyncGenerator[DbConnection | None]:
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
async def transaction(db: DbConnection | None = None) -> AsyncGenerator[DbConnection]:
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
