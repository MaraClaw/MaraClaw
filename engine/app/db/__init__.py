"""Psycopg3 database access layer."""

from app.db.connection import DbConnection
from app.db.errors import DbError, UniqueViolationError, map_psycopg_error
from app.db.pool import close_pool, get_pool, init_pool, ping_pool
from app.db.session import (
    bind_crud_connection,
    connection_ctx,
    get_connection,
    get_db,
    optional_connection_ctx,
    transaction,
)
from app.db.url import normalize_psycopg_conninfo

__all__ = [
    "DbConnection",
    "DbError",
    "UniqueViolationError",
    "bind_crud_connection",
    "close_pool",
    "connection_ctx",
    "get_connection",
    "get_db",
    "get_pool",
    "init_pool",
    "map_psycopg_error",
    "normalize_psycopg_conninfo",
    "optional_connection_ctx",
    "ping_pool",
    "transaction",
]
