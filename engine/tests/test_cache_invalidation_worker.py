"""Publication and pacing of the cache invalidation worker against fakes: no database, no Redis."""

from types import TracebackType
from typing import Self
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError, ResponseError, TimeoutError as RedisTimeoutError

from app.core.events import RedisPoolOverloadError
from app.records.cache_invalidation import CacheInvalidationClaim, CacheInvalidationKind
from app.runtime.tasks import capped_backoff
from app.services import cache_invalidation_worker as worker


class FakePipeline:
    def __init__(self, replies: list[object], failure: Exception | None) -> None:
        self.replies = replies
        self.failure = failure
        self.commands: list[tuple[str, str, int | None, str | None]] = []
        self.raise_on_error: bool | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        return None

    def set(self, key: str, value: str, ex: int | None = None) -> Self:
        self.commands.append(("set", key, ex, value))
        return self

    def delete(self, key: str) -> Self:
        self.commands.append(("delete", key, None, None))
        return self

    async def execute(self, raise_on_error: bool = True) -> list[object]:
        self.raise_on_error = raise_on_error
        if self.failure is not None:
            raise self.failure
        return self.replies


class FakeClient:
    def __init__(self, replies: list[object] | None = None, failure: Exception | None = None) -> None:
        self.replies = replies or []
        self.failure = failure
        self.transactions: list[bool] = []
        self.pipelines: list[FakePipeline] = []

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        self.transactions.append(transaction)
        self.pipelines.append(FakePipeline(self.replies, self.failure))
        return self.pipelines[-1]


def install(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    async def get_client() -> FakeClient:
        return client

    monkeypatch.setattr(worker, "get_cache_redis", get_client)


def claim(kind: CacheInvalidationKind) -> CacheInvalidationClaim:
    return CacheInvalidationClaim(f"key:{uuid4()}", kind, uuid4(), uuid4(), 1)


async def test_a_batch_is_one_non_transactional_pipeline_of_single_key_commands_with_a_fresh_token_per_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(replies=[True, True, True, 1])
    install(monkeypatch, client)
    claims = [claim(kind) for kind in CacheInvalidationKind]

    assert await worker.publish_batch(claims) == [True] * 4
    assert await worker.publish_batch(claims) == [True] * 4

    assert client.transactions == [False, False]
    first, second = client.pipelines
    assert first.raise_on_error is False
    assert [(name, key, ttl) for name, key, ttl, _ in first.commands] == [
        ("set", claims[0].key, 3600),
        ("set", claims[1].key, 3600),
        ("set", claims[2].key, 3600),
        ("delete", claims[3].key, None),
    ]
    tokens = [token for _, _, _, token in (*first.commands, *second.commands) if token is not None]
    assert len(tokens) == len(set(tokens)) == 6
    assert all(len(token) == 32 for token in tokens)


async def test_a_command_error_fails_only_its_own_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeClient(replies=[True, ResponseError("READONLY"), 1]))
    claims = [
        claim(CacheInvalidationKind.VERSION),
        claim(CacheInvalidationKind.ACL_BUMP),
        claim(CacheInvalidationKind.DELETE),
    ]

    assert await worker.publish_batch(claims) == [True, False, True]


@pytest.mark.parametrize(
    "failure",
    [RedisConnectionError("down"), RedisTimeoutError("slow"), RedisPoolOverloadError("busy"), OSError("reset")],
)
async def test_a_failed_pipeline_acknowledges_nothing(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    install(monkeypatch, FakeClient(failure=failure))

    assert (
        await worker.publish_batch([claim(CacheInvalidationKind.VERSION), claim(CacheInvalidationKind.DELETE)]) is None
    )


async def test_an_unreachable_client_acknowledges_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable() -> FakeClient:
        raise RedisPoolOverloadError("no free connection")

    monkeypatch.setattr(worker, "get_cache_redis", unavailable)

    assert await worker.publish_batch([claim(CacheInvalidationKind.VERSION)]) is None


class Stop(BaseException):
    """Ends the loop under test once its script is spent."""


class ScriptedLoop:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *script: worker.ReplayPass | Exception) -> None:
        self.script = list(script)
        self.sleeps: list[float] = []
        self.beats = 0
        monkeypatch.setattr(worker, "replay_cache_invalidations", self.replay)
        monkeypatch.setattr(worker, "sleep", self.sleep)
        monkeypatch.setattr(worker, "loop_progress", lambda: self)

    async def replay(self) -> worker.ReplayPass:
        if not self.script:
            raise Stop
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def beat(self) -> None:
        self.beats += 1


FULL = worker.ReplayPass(claimed=worker.BATCH_SIZE, reached_redis=True)


async def test_only_a_full_batch_that_reached_redis_loops_again_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = ScriptedLoop(
        monkeypatch,
        FULL,
        FULL,
        worker.ReplayPass(claimed=3, reached_redis=True),
        worker.ReplayPass(claimed=worker.BATCH_SIZE, reached_redis=False),
        worker.ReplayPass(claimed=0, reached_redis=False),
    )

    with pytest.raises(Stop):
        await worker.start_cache_invalidation_worker()

    assert loop.sleeps == [worker.POLL_SECONDS] * 3
    assert loop.beats == 5


async def test_a_database_failure_backs_off_without_a_beat_and_a_pass_resets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = ScriptedLoop(
        monkeypatch, TimeoutError(), OSError(), FULL, TimeoutError(), worker.ReplayPass(claimed=0, reached_redis=False)
    )

    with pytest.raises(Stop):
        await worker.start_cache_invalidation_worker()

    backoff = [capped_backoff(failures, base_seconds=1.0, cap_seconds=30.0) for failures in (0, 1, 0)]
    assert loop.sleeps == [backoff[0], backoff[1], backoff[2], worker.POLL_SECONDS]
    assert loop.beats == 2
