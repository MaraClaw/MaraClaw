"""The per-client-IP budget of the public organization admission submit: fixed hourly window, fails open."""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from tests.rate_limit_fakes import NOW, FakeRedis, install_limiter_redis

from app.core import rate_limit
from app.services.organization_admission_rate_limit import (
    SUBMISSIONS_PER_WINDOW,
    WINDOW_SECONDS,
    allow_submission,
)


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def bind(self, **_extra: object) -> RecordingLogger:
        return self

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(message.format(*args))


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    return install_limiter_redis(monkeypatch)


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
    assert recorder.warnings == ["[rate-limit] organization_admission:submit limiter unavailable; allowing the request"]


async def test_a_pipeline_that_fails_midway_allows_the_request_and_logs_a_warning(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    recorder = RecordingLogger()
    redis.broken_on_execute = True
    monkeypatch.setattr(rate_limit, "logger", recorder)

    assert await allow_submission("203.0.113.7", now=NOW)
    assert len(recorder.warnings) == 1


async def test_the_window_key_keeps_its_client_and_hour(redis: FakeRedis):
    _ = await allow_submission("203.0.113.7", now=NOW)

    assert list(redis.counts) == [f"organization_admission:submit:203.0.113.7:{int(NOW.timestamp()) // WINDOW_SECONDS}"]


async def test_a_spent_budget_reports_the_seconds_until_the_window_renews(redis: FakeRedis):
    limit = rate_limit.Limit("test:scope", "subject", 2, 600)

    results = [await rate_limit.consume(limit) for _ in range(4)]

    renews_in = 600 - int(NOW.timestamp()) % 600
    assert results == [None, None, renews_in, renews_in]


async def test_enforce_refuses_with_the_shared_429_and_counts_no_later_limit(redis: FakeRedis):
    address = rate_limit.Limit("test:address", "a", 1, 600)
    account = rate_limit.Limit("test:account", "b", 5, 600)
    await rate_limit.enforce(address, account)

    with pytest.raises(HTTPException) as refused:
        await rate_limit.enforce(address, account)

    assert refused.value.status_code == 429
    assert refused.value.detail == rate_limit.TOO_MANY_REQUESTS_DETAIL
    assert refused.value.headers == {"Retry-After": str(600 - int(NOW.timestamp()) % 600)}
    assert {key.split(":")[1]: count for key, count in redis.counts.items()} == {"address": 2, "account": 1}


async def test_enforce_allows_every_limit_when_redis_is_down(redis: FakeRedis):
    redis.broken_on_execute = True

    await rate_limit.enforce(rate_limit.Limit("test:one", "s", 1, 600), rate_limit.Limit("test:two", "s", 1, 600))
