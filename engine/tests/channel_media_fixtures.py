from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import override

import httpx
import pytest

from app.services.channels.media_limits import READ_CHUNK_BYTES

# Captured at import so a test that patches ``httpx.AsyncClient`` can still build the real one.
REAL_CLIENT = httpx.AsyncClient


class CountingStream(httpx.AsyncByteStream):
    """A body of ``total`` bytes without Content-Length that records how much the reader pulled."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.consumed = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        block = b"x" * READ_CHUNK_BYTES
        while self.consumed < self.total:
            self.consumed += len(block)
            yield block


def serve(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Route every ``httpx.AsyncClient`` the code under test builds to ``handler``."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: REAL_CLIENT(transport=transport))
