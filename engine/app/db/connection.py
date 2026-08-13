"""Thin async connection wrapper over psycopg3."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from psycopg import AsyncConnection, AsyncCursor
from psycopg.rows import DictRow

from app.db.errors import map_psycopg_error

type Params = Mapping[str, Any] | Sequence[Any] | None
type Row = dict[str, Any]


class DbConnection:
    """Application-facing async DB connection.

    Query parameters should use named ``%(name)s`` style for mappings, or
    positional ``%s`` for sequences. Never interpolate user input into SQL.
    """

    def __init__(self, conn: AsyncConnection[DictRow]) -> None:
        self._conn = conn

    @property
    def raw(self) -> AsyncConnection[DictRow]:
        """Expose the underlying psycopg connection for advanced use."""
        return self._conn

    async def execute(self, query: str, params: Params = None) -> None:
        """Execute a statement and discard any result rows."""
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(query, params)
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def executemany(self, query: str, params_seq: Sequence[Params]) -> None:
        """Execute a statement once per params mapping/sequence."""
        try:
            async with self._conn.cursor() as cur:
                await cur.executemany(query, params_seq)
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def fetchone(self, query: str, params: Params = None) -> Row | None:
        """Execute and return one row as a dict, or None."""
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                return dict(row) if row is not None else None
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def fetchall(self, query: str, params: Params = None) -> list[Row]:
        """Execute and return all rows as dicts."""
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def fetchval(self, query: str, params: Params = None, *, column: int | str = 0) -> Any:
        """Execute and return a single column value from the first row."""
        row = await self.fetchone(query, params)
        if row is None:
            return None
        if isinstance(column, int):
            values = list(row.values())
            return values[column] if column < len(values) else None
        return row.get(column)

    async def cursor(self) -> AsyncCursor[DictRow]:
        """Open a raw cursor (caller owns lifecycle via async with)."""
        return self._conn.cursor()

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()
