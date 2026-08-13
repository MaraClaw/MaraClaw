"""psycopg type adapters for application connections."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


def configure_connection(conn: Any) -> None:
    """Apply project-wide connection defaults.

    Called for every pooled connection. Prefer JSONB for dict/list payloads
    written via :class:`psycopg.types.json.Jsonb`.
    """
    # dict_row keeps repository mapping simple for the migration.
    conn.row_factory = dict_row


def as_jsonb(value: Any) -> Jsonb:
    """Wrap a Python value for JSONB bind parameters."""
    return Jsonb(value)
