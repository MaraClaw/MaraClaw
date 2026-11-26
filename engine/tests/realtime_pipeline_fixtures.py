"""Redis pipeline stand-in for realtime presence fakes that implement only the lookups routing reads."""

from collections.abc import Awaitable, Callable
from typing import Protocol, Self


class PresenceReads(Protocol):
    """What a presence fake answers: a connection hash, a session index and a plain value (the ACL version)."""

    def hgetall(self, key: str, /) -> Awaitable[dict[str, str]]: ...

    def zrangebyscore(self, key: str, minimum: str, maximum: str, /) -> Awaitable[list[str]]: ...

    def get(self, key: str, /) -> Awaitable[str | None]: ...


class PresencePipeline:
    """Queue ``HGETALL``, ``ZRANGEBYSCORE`` and ``GET`` the way a Redis pipeline does and answer them from ``reads``.

    ``executed`` is told the command names of every executed pipeline, one call per round trip.
    """

    def __init__(self, reads: PresenceReads, executed: Callable[[list[str]], None] = lambda _commands: None) -> None:
        self._reads = reads
        self._executed = executed
        self._queued: list[tuple[str, Callable[[], Awaitable[object]]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def hgetall(self, key: str) -> Self:
        self._queued.append(("HGETALL", lambda: self._reads.hgetall(key)))
        return self

    def zrangebyscore(self, key: str, minimum: str, maximum: str) -> Self:
        self._queued.append(("ZRANGEBYSCORE", lambda: self._reads.zrangebyscore(key, minimum, maximum)))
        return self

    def get(self, key: str) -> Self:
        self._queued.append(("GET", lambda: self._reads.get(key)))
        return self

    async def execute(self) -> list[object]:
        self._executed([name for name, _ in self._queued])
        return [await read() for _, read in self._queued]
