"""Single-slot pool fake for transaction ownership and contention tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Self, cast

from psycopg import AsyncConnection
from psycopg.rows import DictRow

LOCK_TIMEOUT_SQL = "SELECT set_config('lock_timeout', %(ms)s, false)"


class _LockTimeoutCursor:
    """Cursor that accepts only the checkout-time request ``lock_timeout`` statement a bound request issues."""

    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: object = None) -> None:
        assert query == LOCK_TIMEOUT_SQL
        assert isinstance(params, dict)
        self._executed.append(query)


class CountingRawConnection:
    """Accumulate transaction outcomes while tracking pending work."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.in_transaction = False
        self.executed: list[str] = []

    def cursor(self) -> _LockTimeoutCursor:
        return _LockTimeoutCursor(self.executed)

    def begin(self) -> None:
        self.in_transaction = True

    async def commit(self) -> None:
        self.commits += 1
        self.in_transaction = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.in_transaction = False


class CountingPool:
    """Serialize borrowers and count ownership independently of commits."""

    def __init__(self) -> None:
        self.raw = CountingRawConnection()
        self.active = 0
        self.checkouts = 0
        self.releases = 0
        self.waiting = asyncio.Event()
        self._slot = asyncio.Lock()

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection[DictRow]]:
        if self._slot.locked():
            self.waiting.set()
        async with self._slot:
            self.active += 1
            self.checkouts += 1
            self.raw.begin()
            try:
                yield cast("AsyncConnection[DictRow]", self.raw)
            finally:
                if self.raw.in_transaction:
                    await self.raw.rollback()
                self.active -= 1
                self.releases += 1
