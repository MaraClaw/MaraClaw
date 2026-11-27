"""A fake Redis for the shared limiter: INCR + EXPIRE pipelines, a fixed clock and a switch that makes it fail."""

from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from app.core import rate_limit

NOW = datetime(2030, 1, 1, 10, 30, tzinfo=UTC)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, str, int | None]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        return None

    def incr(self, key: str) -> FakePipeline:
        self.commands.append(("incr", key, None))
        return self

    def expire(self, key: str, seconds: int) -> FakePipeline:
        self.commands.append(("expire", key, seconds))
        return self

    async def execute(self) -> list[int | bool]:
        if self.redis.broken_on_execute:
            raise ConnectionError("redis went away")
        results: list[int | bool] = []
        for name, key, seconds in self.commands:
            if name == "incr":
                self.redis.counts[key] = self.redis.counts.get(key, 0) + 1
                results.append(self.redis.counts[key])
            else:
                assert seconds is not None
                self.redis.ttls[key] = seconds
                results.append(True)
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.broken_on_execute = False

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction
        return FakePipeline(self)


def install_limiter_redis(monkeypatch: pytest.MonkeyPatch, *, now: datetime = NOW) -> FakeRedis:
    """Route the shared limiter to a fake Redis and freeze its clock."""
    fake = FakeRedis()

    async def get_redis() -> FakeRedis:
        return fake

    monkeypatch.setattr(rate_limit, "get_redis", get_redis)
    monkeypatch.setattr(rate_limit, "_now", lambda: now)
    return fake
