from collections.abc import Iterator, Mapping
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from app.db import pool as pool_module, session as session_module
from app.db.connection import Params, Row
from app.records.notification import NotificationRecord


class NotificationConnection:
    def __init__(self) -> None:
        self.notifications: list[NotificationRecord] = []
        self.executed: list[tuple[str, Mapping[str, object]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _Cursor:
    def __init__(self, raw: NotificationConnection) -> None:
        self.raw: NotificationConnection = raw
        self.rows: list[Row] = []

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Params = None) -> None:
        assert isinstance(params, Mapping)
        sql = " ".join(query.split())
        self.raw.executed.append((sql, dict(params)))
        types = params.get("types", [])
        assert isinstance(types, list)
        matching = [
            row
            for row in self.raw.notifications
            if ("user_id = %(user_id)s" not in sql or row.user_id == params["user_id"])
            and ("is_read IS FALSE" not in sql or not row.is_read)
            and ("type = ANY(%(types)s)" not in sql or row.type in types)
        ]
        if sql.startswith("UPDATE notifications SET is_read = TRUE"):
            for row in matching:
                row.is_read = True
            return
        if sql.startswith("SELECT COUNT(*)"):
            self.rows = [{"count": len(matching)}]
            return
        assert sql.startswith("SELECT ")
        if "ORDER BY created_at DESC, id DESC" in sql:
            matching.sort(key=lambda row: (row.created_at or datetime.min.replace(tzinfo=UTC), row.id), reverse=True)
        elif "ORDER BY created_at DESC" in sql:
            matching.sort(key=lambda row: row.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        offset, limit = params.get("offset", 0), params.get("limit", len(matching))
        assert isinstance(offset, int)
        assert isinstance(limit, int)
        self.rows = [asdict(row) for row in matching[offset : offset + limit]]

    async def fetchone(self) -> Row | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[Row]:
        return self.rows.copy()


class _PoolConnection:
    def __init__(self, raw: NotificationConnection) -> None:
        self.raw: NotificationConnection = raw

    async def __aenter__(self) -> NotificationConnection:
        return self.raw

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _Pool:
    def __init__(self, raw: NotificationConnection) -> None:
        self.raw: NotificationConnection = raw

    def connection(self) -> _PoolConnection:
        return _PoolConnection(self.raw)


@pytest.fixture
def notification_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[NotificationConnection]:
    raw = NotificationConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _Pool(raw))
    token = session_module._conn_ctx.set(None)
    try:
        yield raw
    finally:
        session_module._conn_ctx.reset(token)
        pool_module.reset_pool_for_tests()
