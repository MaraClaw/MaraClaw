"""psycopg type adapters for application connections."""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb


def configure_connection(conn: AsyncConnection[DictRow]) -> None:
    """Apply project-wide connection defaults.

    Called for every pooled connection. Prefer JSONB for dict/list payloads
    written via :class:`psycopg.types.json.Jsonb`.
    """
    # dict_row keeps repository mapping simple for the migration.
    conn.row_factory = dict_row


def as_jsonb(value: object) -> Jsonb:
    """Wrap a Python value for JSONB bind parameters."""
    return Jsonb(value)
