"""Redis pipeline stand-in for realtime presence fakes that only implement ``HGETALL``."""

from collections.abc import Awaitable, Callable
from typing import Self


class HashPipeline:
    """Queue ``HGETALL`` commands the way a Redis pipeline does and answer them from ``read``."""

    def __init__(self, read: Callable[[str], Awaitable[dict[str, str]]]) -> None:
        self._read: Callable[[str], Awaitable[dict[str, str]]] = read
        self._keys: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def hgetall(self, key: str) -> Self:
        self._keys.append(key)
        return self

    async def execute(self) -> list[dict[str, str]]:
        return [await self._read(key) for key in self._keys]
