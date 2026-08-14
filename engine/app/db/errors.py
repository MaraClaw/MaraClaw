"""Database error types and psycopg error mapping."""

from __future__ import annotations


class DbError(Exception):
    """Base class for application database errors."""

    def __init__(self, message: str, *, orig: BaseException | None = None) -> None:
        super().__init__(message)
        self.orig: BaseException | None = orig


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


def map_psycopg_error(exc: BaseException) -> DbError:
    """Map a psycopg exception to a typed application error."""
    # Import lazily so modules can load even if binary import is deferred in tests.
    from psycopg import errors as pg_errors

    if isinstance(exc, pg_errors.UniqueViolation):
        return UniqueViolationError(constraint=_constraint_name(exc), orig=exc)
    if isinstance(exc, pg_errors.ForeignKeyViolation):
        return ForeignKeyViolationError(constraint=_constraint_name(exc), orig=exc)
    if isinstance(exc, pg_errors.CheckViolation):
        return CheckViolationError(constraint=_constraint_name(exc), orig=exc)
    if isinstance(exc, pg_errors.Error):
        return DbError(str(exc) or exc.__class__.__name__, orig=exc)
    return DbError(str(exc) or exc.__class__.__name__, orig=exc)
