"""Fake monotonic clock and parked sleep for worker progress-budget tests; no real time passes."""

import asyncio


class FakeClock:
    def __init__(self) -> None:
        self.now: float = 0.0

    def __call__(self) -> float:
        return self.now


class Pacer:
    """Replaces a worker's sleep: each wait parks until the test releases it, so no real time passes."""

    def __init__(self) -> None:
        self.waits: list[float] = []
        self._arrived: asyncio.Queue[None] = asyncio.Queue()
        self._released: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self._arrived.put_nowait(None)
        await self._released.get()

    async def parked(self) -> None:
        await asyncio.wait_for(self._arrived.get(), 5)

    async def release(self) -> None:
        self._released.put_nowait(None)
        await self.parked()
