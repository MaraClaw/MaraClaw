"""Single-slot pool fake for transaction ownership and contention tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from psycopg import AsyncConnection
from psycopg.rows import DictRow


class CountingRawConnection:
    """Accumulate transaction outcomes while tracking pending work."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.in_transaction = False

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
                yield cast(AsyncConnection[DictRow], self.raw)
            finally:
                if self.raw.in_transaction:
                    await self.raw.rollback()
                self.active -= 1
                self.releases += 1
