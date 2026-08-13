"""Legacy compatibility shims for database access.

Runtime data access uses ``app.db`` (psycopg pool) and ``app.dao``.
Prefer ``app.db.session.connection_ctx`` / ``transaction`` for new code.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


def async_session(*_args, **_kwargs):
    """Removed SQLAlchemy session factory; patchable for legacy tests."""
    raise RuntimeError("app.database.async_session was removed; use app.db / DAOs")


__all__ = ["async_session", "transaction"]


@asynccontextmanager
async def transaction(session: Any = None) -> AsyncIterator[Any]:
    """Join psycopg ``app.db.session.transaction`` when no session is provided.

    If a caller still passes an explicit session object, it is yielded as-is.
    """
    if session is not None:
        yield session
        return

    from app.db.session import transaction as psycopg_transaction

    async with psycopg_transaction() as conn:
        yield conn
