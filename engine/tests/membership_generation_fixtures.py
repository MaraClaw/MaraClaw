"""Doubles for the membership generation: a pinned SQL read and a recorder for UserDAO writes."""

from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Self
from unittest.mock import AsyncMock

import pytest

from app.dao.user_dao import user_dao
from app.db import session as session_module
from app.records.user import UserRecord


def pin_membership_generation(monkeypatch: pytest.MonkeyPatch, value: int | None = 0) -> AsyncMock:
    """Make the membership read return ``value`` (0 matches tokens minted by the same fixture)."""
    read = AsyncMock(return_value=value)
    monkeypatch.setattr(user_dao, "get_membership_generation", read)
    return read


class _Cursor:
    def __init__(self, parent: RecordingUserConnection) -> None:
        self._parent = parent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def execute(self, query: str, params: Any = None) -> None:
        self._parent.executed.append((query, params or {}))

    async def executemany(self, query: str, params_seq: Sequence[Any]) -> None:
        for params in params_seq:
            await self.execute(query, params)

    async def fetchone(self) -> dict[str, object]:
        query, params = self._parent.executed[-1]
        user = self._parent.user
        if "FROM tenants" in query:
            return {"id": params["id"]}
        if query.startswith("SELECT membership_generation"):
            return {"membership_generation": self._parent.generation}
        if query.startswith("SELECT tenant_id FROM users"):
            return {"tenant_id": user.tenant_id}
        is_active = user.is_active
        if query.startswith("UPDATE users"):
            is_active = bool(params["is_active"]) if "is_active" in params else "SET is_active = FALSE" not in query
        return {
            "id": user.id,
            "identity_id": user.identity_id,
            "tenant_id": user.tenant_id,
            "display_name": user.display_name,
            "role": str(user.role),
            "is_active": is_active,
            "registration_source": "web",
        }

    async def fetchall(self) -> list[dict[str, object]]:
        query, _params = self._parent.executed[-1]
        return [{"id": self._parent.user.id}] if query.startswith("UPDATE users") else []


class RecordingUserConnection:
    """Raw psycopg connection double: records every statement and answers user reads with one membership row."""

    def __init__(self, user: UserRecord) -> None:
        self.user = user
        self.generation = 0
        self.executed: list[tuple[str, Mapping[str, object]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def connection(self) -> RecordingUserConnection:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    def updates_to(self, table: str) -> list[tuple[str, Mapping[str, object]]]:
        return [(query, params) for query, params in self.executed if query.lstrip().startswith(f"UPDATE {table} ")]


@contextmanager
def recording_user_writes(monkeypatch: pytest.MonkeyPatch, user: UserRecord) -> Generator[RecordingUserConnection]:
    """Route the pool to a recorder for the duration of the block."""
    raw = RecordingUserConnection(user)
    monkeypatch.setattr(session_module, "get_pool", lambda: raw)
    token = session_module._conn_ctx.set(None)
    try:
        yield raw
    finally:
        session_module._conn_ctx.reset(token)
