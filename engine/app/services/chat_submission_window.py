"""In-memory sliding window of one chat socket's submissions; it never touches Redis or the database."""

import time
from collections import deque
from collections.abc import Callable
from typing import final


@final
class SubmissionWindow:
    """Admit at most ``limit`` submissions in any ``window_seconds`` span, judged by an injectable clock.

    Only admitted submissions are remembered, so the window holds at most ``limit`` timestamps however hard a client
    hammers, and a refused one is admitted again as soon as the oldest admitted one leaves the window. The window
    belongs to one socket: nothing is shared across sockets or processes.
    """

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._arrivals: deque[float] = deque()

    def admit(self) -> bool:
        now = self._clock()
        while self._arrivals and now - self._arrivals[0] >= self._window_seconds:
            _ = self._arrivals.popleft()
        if len(self._arrivals) >= self._limit:
            return False
        self._arrivals.append(now)
        return True
