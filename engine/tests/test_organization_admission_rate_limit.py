"""The per-client-IP budget of the public organization admission submit: fixed hourly window, fails open."""

from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from app.services import organization_admission_rate_limit as rate_limit
from app.services.organization_admission_rate_limit import (
    SUBMISSIONS_PER_WINDOW,
    WINDOW_SECONDS,
    allow_submission,
)

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


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def bind(self, **_extra: object) -> RecordingLogger:
        return self

    def warning(self, message: str) -> None:
        self.warnings.append(message)


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()

    async def get_redis() -> FakeRedis:
        return fake

    monkeypatch.setattr(rate_limit, "get_redis", get_redis)
    return fake


async def test_five_submissions_per_hour_are_allowed_and_the_sixth_is_not(redis: FakeRedis):
    results = [await allow_submission("203.0.113.7", now=NOW) for _ in range(SUBMISSIONS_PER_WINDOW + 2)]

    assert results == [True] * SUBMISSIONS_PER_WINDOW + [False, False]
    assert SUBMISSIONS_PER_WINDOW == 5


async def test_each_client_has_its_own_budget_and_unknown_clients_share_one(redis: FakeRedis):
    for _ in range(SUBMISSIONS_PER_WINDOW):
        assert await allow_submission("203.0.113.7", now=NOW)

    assert not await allow_submission("203.0.113.7", now=NOW)
    assert await allow_submission("203.0.113.8", now=NOW)
    for _ in range(SUBMISSIONS_PER_WINDOW):
        assert await allow_submission(None, now=NOW)
    assert not await allow_submission(None, now=NOW)


async def test_the_budget_renews_when_the_hour_changes(redis: FakeRedis):
    for _ in range(SUBMISSIONS_PER_WINDOW + 1):
        _ = await allow_submission("203.0.113.7", now=NOW)

    assert not await allow_submission("203.0.113.7", now=NOW + timedelta(minutes=29))
    assert await allow_submission("203.0.113.7", now=NOW + timedelta(minutes=31))


async def test_every_window_key_expires_with_the_window(redis: FakeRedis):
    _ = await allow_submission("203.0.113.7", now=NOW)

    assert list(redis.ttls.values()) == [WINDOW_SECONDS]
    assert WINDOW_SECONDS == 3600


async def test_an_unreachable_redis_allows_the_request_and_logs_a_warning(monkeypatch: pytest.MonkeyPatch):
    recorder = RecordingLogger()

    async def get_redis() -> FakeRedis:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rate_limit, "get_redis", get_redis)
    monkeypatch.setattr(rate_limit, "logger", recorder)

    assert await allow_submission("203.0.113.7", now=NOW)
    assert recorder.warnings == ["[organization-admission] submit rate limiter unavailable; allowing the request"]


async def test_a_pipeline_that_fails_midway_allows_the_request_and_logs_a_warning(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    recorder = RecordingLogger()
    redis.broken_on_execute = True
    monkeypatch.setattr(rate_limit, "logger", recorder)

    assert await allow_submission("203.0.113.7", now=NOW)
    assert len(recorder.warnings) == 1
