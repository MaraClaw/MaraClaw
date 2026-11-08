"""Database error types and psycopg error mapping."""

from __future__ import annotations

from typing import ClassVar


class DbError(Exception):
    """Base class for application database errors."""

    def __init__(self, message: str, *, orig: BaseException | None = None) -> None:
        super().__init__(message)
        self.orig: BaseException | None = orig


class DbOverloadError(DbError):
    """Retryable database overload; HTTP boundaries answer 503 with ``Retry-After``."""

    retry_after_seconds: ClassVar[int] = 5

    def __init__(self, message: str = "database overloaded", *, orig: BaseException | None = None) -> None:
        super().__init__(message, orig=orig)


class PoolCheckoutTimeoutError(DbOverloadError):
    """Raised when no pooled connection became available within ``DATABASE_POOL_TIMEOUT``."""

    def __init__(
        self, message: str = "database connection pool timed out", *, orig: BaseException | None = None
    ) -> None:
        super().__init__(message, orig=orig)


class QueryCanceledError(DbOverloadError):
    """Raised when the server cancels a statement (``statement_timeout`` or a cancel request)."""

    def __init__(self, message: str = "database statement canceled", *, orig: BaseException | None = None) -> None:
        super().__init__(message, orig=orig)


class LockNotAvailableError(DbError):
    """Raised when a requested row lock cannot be acquired immediately."""

    def __init__(self, message: str = "lock not available", *, orig: BaseException | None = None) -> None:
        super().__init__(message, orig=orig)


class UniqueViolationError(DbError):
    """Raised when a unique constraint is violated."""

    def __init__(
        self,
        message: str = "unique constraint violated",
        *,
        constraint: str | None = None,
        orig: BaseException | None = None,
    ) -> None:
        super().__init__(message, orig=orig)
        self.constraint: str | None = constraint


class ForeignKeyViolationError(DbError):
    """Raised when a foreign key constraint is violated."""

    def __init__(
        self,
        message: str = "foreign key constraint violated",
        *,
        constraint: str | None = None,
        orig: BaseException | None = None,
    ) -> None:
        super().__init__(message, orig=orig)
        self.constraint: str | None = constraint


class CheckViolationError(DbError):
    """Raised when a check constraint is violated."""

    def __init__(
        self,
        message: str = "check constraint violated",
        *,
        constraint: str | None = None,
        orig: BaseException | None = None,
    ) -> None:
        super().__init__(message, orig=orig)
        self.constraint: str | None = constraint


def _constraint_name(exc: object) -> str | None:
    from app.core.json_types import object_attr

    diag = object_attr(exc, "diag")
    if diag is None:
        return None
    name = object_attr(diag, "constraint_name")
    return str(name) if name else None


def overload_error(exc: BaseException) -> DbOverloadError | None:
    """Return the typed overload error for ``exc``, or None when it is not an overload.

    Recognises a checkout ``PoolTimeout`` and a server ``QueryCanceled``, raw or as ``DbError.orig``, so callers that
    bypass :func:`map_psycopg_error` (commit, rollback, direct ``get_pool().connection()``) classify the same way.
    """
    if isinstance(exc, DbOverloadError):
        return exc
    from psycopg import errors as pg_errors
    from psycopg_pool import PoolTimeout

    source = exc.orig if isinstance(exc, DbError) else exc
    if isinstance(source, PoolTimeout):
        return PoolCheckoutTimeoutError(orig=source)
    if isinstance(source, pg_errors.QueryCanceled):
        return QueryCanceledError(orig=source)
    return None


def map_psycopg_error(exc: BaseException) -> DbError:
    """Map a psycopg exception to a typed application error."""
    # Import lazily so modules can load even if binary import is deferred in tests.
    from psycopg import errors as pg_errors

    overload = overload_error(exc)
    if overload is not None:
        return overload
    if isinstance(exc, pg_errors.LockNotAvailable):
        return LockNotAvailableError(orig=exc)
    if isinstance(exc, pg_errors.UniqueViolation):
        return UniqueViolationError(constraint=_constraint_name(exc), orig=exc)
    if isinstance(exc, pg_errors.ForeignKeyViolation):
        return ForeignKeyViolationError(constraint=_constraint_name(exc), orig=exc)
    if isinstance(exc, pg_errors.CheckViolation):
        return CheckViolationError(constraint=_constraint_name(exc), orig=exc)
    if isinstance(exc, pg_errors.Error):
        return DbError(str(exc) or exc.__class__.__name__, orig=exc)
    return DbError(str(exc) or exc.__class__.__name__, orig=exc)
