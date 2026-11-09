"""Thin async connection wrapper over psycopg3."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from contextlib import suppress
from typing import cast

import anyio
from psycopg import AsyncConnection, AsyncCursor
from psycopg.abc import Params as PgParams, QueryNoTemplate
from psycopg.rows import DictRow

from app.db.errors import map_psycopg_error, overload_error

type Params = Mapping[str, object] | Sequence[object] | None
type Row = dict[str, object]


async def _await_psycopg[T](operation: Coroutine[object, object, T]) -> T:
    # psycopg needs one edge cancellation followed by uninterrupted cancel/drain I/O.
    # An unregistered asyncio task isolates it from AnyIO's repeated cancellation.
    task = asyncio.create_task(operation, eager_start=True)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _ = task.cancel()
        with anyio.CancelScope(shield=True):
            # Wait without forwarding native cancellation or raising a competing query error.
            while not task.done():
                with suppress(asyncio.CancelledError):
                    _ = await asyncio.wait((task,))
            if not task.cancelled():
                _ = task.exception()
        raise


def _mapping_row(raw: object) -> Row:
    if not isinstance(raw, Mapping):
        return {}
    mapping = cast("Mapping[object, object]", raw)
    return {str(key): value for key, value in mapping.items()}


class DbConnection:
    """Application-facing async DB connection.

    Query parameters should use named ``%(name)s`` style for mappings, or
    positional ``%s`` for sequences. Never interpolate user input into SQL.
    """

    def __init__(self, conn: AsyncConnection[DictRow]) -> None:
        self._conn: AsyncConnection[DictRow] = conn

    @property
    def raw(self) -> AsyncConnection[DictRow]:
        """Expose the underlying psycopg connection for advanced use."""
        return self._conn

    async def execute(self, query: str, params: Params = None) -> None:
        """Execute a statement and discard any result rows."""
        try:
            async with self._conn.cursor() as cur:
                _ = await _await_psycopg(cur.execute(cast("QueryNoTemplate", query), params))
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def executemany(self, query: str, params_seq: Sequence[Params]) -> None:
        """Execute a statement once per params mapping/sequence."""
        try:
            async with self._conn.cursor() as cur:
                await _await_psycopg(
                    cur.executemany(cast("QueryNoTemplate", query), cast("Iterable[PgParams]", params_seq))
                )
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def fetchone(self, query: str, params: Params = None) -> Row | None:
        """Execute and return one row as a dict, or None."""
        try:
            async with self._conn.cursor() as cur:
                _ = await _await_psycopg(cur.execute(cast("QueryNoTemplate", query), params))
                row = await _await_psycopg(cur.fetchone())
                return _mapping_row(row) if row is not None else None
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def fetchall(self, query: str, params: Params = None) -> list[Row]:
        """Execute and return all rows as dicts."""
        try:
            async with self._conn.cursor() as cur:
                _ = await _await_psycopg(cur.execute(cast("QueryNoTemplate", query), params))
                rows = await _await_psycopg(cur.fetchall())
                return [_mapping_row(row) for row in rows]
        except Exception as exc:
            raise map_psycopg_error(exc) from exc

    async def fetchval(self, query: str, params: Params = None, *, column: int | str = 0) -> object:
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
        try:
            await _await_psycopg(self._conn.commit())
        except Exception as exc:
            overload = overload_error(exc)
            if overload is None:
                raise
            raise overload from exc

    async def rollback(self) -> None:
        try:
            await _await_psycopg(self._conn.rollback())
        except Exception as exc:
            overload = overload_error(exc)
            if overload is None:
                raise
            raise overload from exc
