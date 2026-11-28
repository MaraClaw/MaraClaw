"""The running loop's clock, frozen until a test moves it, for deadlines that must fire without anything sleeping."""

import asyncio

import pytest

SETTLE_TURNS = 10


class LoopClock:
    """The running loop's ``time()``, held at the moment of creation until ``jump`` or ``advance`` moves it."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop: asyncio.AbstractEventLoop = loop
        self.now: float = loop.time()

    def __call__(self) -> float:
        return self.now

    def jump(self, seconds: float) -> None:
        """Move the clock without yielding, as a source that answers without suspending would see it pass."""
        self.now += seconds

    async def advance(self, seconds: float) -> None:
        """Move the clock, wait until the loop has fired every timer that came due, then let what they started run.

        Nothing real is waited for, so a deadline that fails to fire leaves its task unfinished for the caller to assert
        on instead of hanging the test.
        """
        self.jump(seconds)
        processed = asyncio.Event()
        _ = self._loop.call_at(self.now, processed.set)
        await processed.wait()
        for _turn in range(SETTLE_TURNS):
            await asyncio.sleep(0)


def freeze_loop_clock(monkeypatch: pytest.MonkeyPatch) -> LoopClock:
    """Freeze the running loop's clock (as ``tests/test_heartbeat.py`` does for its run budget) and return its handle."""
    loop = asyncio.get_running_loop()
    clock = LoopClock(loop)
    monkeypatch.setattr(loop, "time", clock)
    return clock
