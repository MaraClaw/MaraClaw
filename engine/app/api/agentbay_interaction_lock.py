"""Serialize Take Control actions by session while the session has holders or waiters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from uuid import UUID

type InteractionKey = tuple[UUID, str]


@dataclass(slots=True)
class InteractionEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@dataclass(frozen=True, slots=True)
class InteractionLock:
    registry: dict[InteractionKey, InteractionEntry]
    key: InteractionKey

    async def acquire(self) -> bool:
        # Resolve at acquisition, not lookup: a saved handle must join the current lock even
        # if an earlier entry for this key has already been retired.
        entry = self.registry.get(self.key)
        if entry is None:
            entry = self.registry[self.key] = InteractionEntry()
        entry.users += 1
        acquired = False
        try:
            acquired = await entry.lock.acquire()
            return acquired
        finally:
            if not acquired:
                self._drop_user(entry)

    def release(self) -> None:
        entry = self.registry.get(self.key)
        if entry is None:
            raise RuntimeError("Lock is not acquired.")
        entry.lock.release()
        self._drop_user(entry)

    def locked(self) -> bool:
        entry = self.registry.get(self.key)
        return entry is not None and entry.lock.locked()

    def _drop_user(self, entry: InteractionEntry) -> None:
        entry.users -= 1
        if entry.users == 0:
            del self.registry[self.key]

    async def __aenter__(self) -> None:
        _ = await self.acquire()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.release()
