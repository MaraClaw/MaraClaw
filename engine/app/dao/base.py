"""Base DAO for pure-psycopg repositories.

Table/column names are class constants, not user input; ruff S608 is therefore
suppressed for the SQL builders in this module.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import MISSING, fields, is_dataclass
from typing import Any
from uuid import UUID, uuid4

from app.db.connection import DbConnection
from app.db.session import connection_ctx
from app.db.types import as_jsonb


class BaseDAO[RecordT]:
    """Shared CRUD helpers over a single table of plain records."""

    table: str
    pk: str = "id"
    columns: tuple[str, ...] = ()
    record_factory: Callable[[dict[str, Any]], RecordT]

    def __init__(self) -> None:
        if not self.table or not self.columns or not getattr(self, "record_factory", None):
            raise TypeError(f"{type(self).__name__} must define table, columns, and record_factory")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[DbConnection]:
        """Yield a context-bound psycopg connection (joins outer transaction when present)."""
        async with connection_ctx() as db:
            yield db

    def _select_list(self, alias: str | None = None) -> str:
        if alias:
            return ", ".join(f"{alias}.{col}" for col in self.columns)
        return ", ".join(self.columns)

    async def get(self, id: Any) -> RecordT | None:
        """Fetch one row by primary key."""
        async with self.session() as db:
            row = await db.fetchone(
                f"SELECT {self._select_list()} FROM {self.table} WHERE {self.pk} = %(id)s",
                {"id": id},
            )
            return self.record_factory(row) if row else None

    async def get_many(self, ids: Sequence[Any]) -> list[RecordT]:
        """Fetch rows whose primary keys are in ``ids`` (order not preserved)."""
        if not ids:
            return []
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} WHERE {self.pk} = ANY(%(ids)s)",
                {"ids": list(ids)},
            )
            return [self.record_factory(row) for row in rows]

    async def is_empty(self) -> bool:
        """Return True when the table has no rows."""
        async with self.session() as db:
            value = await db.fetchval(f"SELECT 1 FROM {self.table} LIMIT 1")
            return value is None

    async def get_all(self, skip: int = 0, limit: int = 100) -> Sequence[RecordT]:
        """Fetch a page of rows."""
        async with self.session() as db:
            rows = await db.fetchall(
                f"SELECT {self._select_list()} FROM {self.table} ORDER BY {self.pk} OFFSET %(skip)s LIMIT %(limit)s",
                {"skip": skip, "limit": limit},
            )
            return [self.record_factory(row) for row in rows]

    def _record_defaults(self) -> dict[str, Any]:
        """Return dataclass field defaults for columns this DAO owns."""
        record_cls = getattr(self.record_factory, "__self__", None)
        if record_cls is None or not is_dataclass(record_cls):
            return {}
        defaults: dict[str, Any] = {}
        for item in fields(record_cls):
            if item.name not in self.columns:
                continue
            if item.default is not MISSING:
                defaults[item.name] = item.default
            elif item.default_factory is not MISSING:  # type: ignore[misc]
                defaults[item.name] = item.default_factory()
        return defaults

    async def create(self, *, obj_in: Mapping[str, Any]) -> RecordT:
        """Insert a row and return the created record."""
        data = dict(obj_in)
        for column, value in self._record_defaults().items():
            if column not in data and value is not None:
                data[column] = value
        if self.pk not in data and self.pk == "id":
            data[self.pk] = uuid4()
        cols = list(dict.fromkeys([c for c in data if c in self.columns or c == self.pk]))
        if not cols:
            raise ValueError("create() requires at least one column value")
        params: dict[str, Any] = {}
        for col in cols:
            value = data[col]
            if isinstance(value, (dict, list)):
                params[col] = as_jsonb(value)
            else:
                params[col] = value
        col_sql = ", ".join(cols)
        val_sql = ", ".join(f"%({c})s" for c in cols)
        async with self.session() as db:
            row = await db.fetchone(
                f"INSERT INTO {self.table} ({col_sql}) VALUES ({val_sql}) RETURNING {self._select_list()}",
                params,
            )
            if row is None:
                raise RuntimeError(f"INSERT into {self.table} returned no row")
            return self.record_factory(row)

    async def update(self, *, db_obj: RecordT, obj_in: Mapping[str, Any]) -> RecordT:
        """Update columns on an existing record and return the refreshed row."""
        data = {k: v for k, v in obj_in.items() if k in self.columns and k != self.pk}
        if not data:
            return db_obj
        pk_value = getattr(db_obj, self.pk)
        params: dict[str, Any] = {self.pk: pk_value}
        assignments: list[str] = []
        for col, value in data.items():
            params[col] = as_jsonb(value) if isinstance(value, (dict, list)) else value
            assignments.append(f"{col} = %({col})s")
        if "updated_at" in self.columns and "updated_at" not in data:
            assignments.append("updated_at = NOW()")
        async with self.session() as db:
            row = await db.fetchone(
                f"UPDATE {self.table} SET {', '.join(assignments)} "
                f"WHERE {self.pk} = %({self.pk})s RETURNING {self._select_list()}",
                params,
            )
            if row is None:
                return db_obj
            return self.record_factory(row)

    async def delete(self, *, id: Any) -> RecordT | None:
        """Delete by primary key and return the deleted row when present."""
        async with self.session() as db:
            row = await db.fetchone(
                f"DELETE FROM {self.table} WHERE {self.pk} = %(id)s RETURNING {self._select_list()}",
                {"id": id},
            )
            return self.record_factory(row) if row else None


def as_uuid(value: Any) -> UUID | None:
    """Normalize optional UUID-ish values."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    return UUID(str(value))
