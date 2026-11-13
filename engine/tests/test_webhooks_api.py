import hashlib
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api import webhooks as webhooks_api
from app.core.logging import configure_logging
from app.main import app

CANARY_TOKEN = "cnry_tkn_91"  # eleven characters: the legacy shape, still accepted
MINTED_TOKEN = "Ab1_-" * 4 + "Ab"  # twenty-two characters: the shape minted from 16 random bytes


class FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else [self._value]


class FakeSession:
    def __init__(self, triggers=None, agent=None):
        self.triggers = triggers or []
        self.agent = agent
        self.added = []
        self.committed = False
        self.expunged = []

    async def execute(self, statement):
        stmt_str = str(statement)
        if "agent_triggers" in stmt_str:
            return FakeScalarResult(self.triggers)
        if "agents" in stmt_str:
            return FakeScalarResult(self.agent)
        return FakeScalarResult(None)

    def add(self, value):
        self.added.append(value)

    def expunge(self, value):
        self.expunged.append(value)

    async def commit(self):
        self.committed = True


class FakeAsyncSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class RecordingPipeline:
    def __init__(self):
        self.zadd_entries = None
        self.keys = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def zremrangebyscore(self, key, _minimum, _maximum):
        self.keys.append(key)

    def zadd(self, key, entries):
        self.keys.append(key)
        self.zadd_entries = entries

    def zcard(self, key):
        self.keys.append(key)

    def expire(self, key, _seconds):
        self.keys.append(key)

    async def execute(self):
        return None, None, 1, None


class RecordingRedis:
    def __init__(self):
        self.pipeline_instance = RecordingPipeline()

    def pipeline(self, *, transaction):
        assert transaction is True
        return self.pipeline_instance


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)

    async def _build():
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _build


@pytest.mark.asyncio
async def test_rate_limit_member_uses_sha256(monkeypatch):
    # Given: a fixed trigger, timestamp, and Redis pipeline
    trigger_id = uuid.uuid4()
    now = 1_700_000_000.0
    redis = RecordingRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(webhooks_api, "get_redis", fake_get_redis)
    monkeypatch.setattr(webhooks_api.time, "time", lambda: now)

    # When: recording a webhook hit
    count = await webhooks_api._record_and_count_hits(trigger_id)

    # Then: the collision-resistant member suffix uses SHA-256 and every key belongs to the trigger
    expected_member = f"{now}:{hashlib.sha256(f'{trigger_id}:{now}'.encode()).hexdigest()[:8]}"
    assert redis.pipeline_instance.zadd_entries == {expected_member: now}
    assert set(redis.pipeline_instance.keys) == {f"webhook:rate:{trigger_id}"}
    assert count == 1


@pytest.mark.asyncio
async def test_count_from_pipeline_fail_closed_on_short_result():
    class ShortPipe:
        async def execute(self):
            return (None, None)

    assert await webhooks_api._count_from_pipeline(ShortPipe()) == 60


@pytest.mark.asyncio
async def test_count_from_pipeline_fail_closed_on_non_sequence():
    class BadPipe:
        async def execute(self):
            return None

    assert await webhooks_api._count_from_pipeline(BadPipe()) == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["valid_token", MINTED_TOKEN])
async def test_receive_webhook_success(monkeypatch, client, token):
    # Setup test trigger and agent
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test-trigger",
        type="webhook",
        config={"token": token},
        is_enabled=True,
    )
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5)

    lookup = AsyncMock(return_value=trigger)
    monkeypatch.setattr(webhooks_api.agent_trigger_dao, "find_webhook_by_token", lookup)
    monkeypatch.setattr(webhooks_api.agent_dao, "get", AsyncMock(return_value=agent))

    # Mock redis rate limiting
    async def fake_record_and_count_hits(trigger_id):
        return 1

    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)

    # Mock enqueue_webhook_execution
    async def fake_enqueue_webhook_execution(*, trigger, body, payload_text, payload_obj, request_headers):
        return SimpleNamespace(id=uuid.uuid4()), True

    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", fake_enqueue_webhook_execution)

    async with await client() as ac:
        response = await ac.post(f"/api/webhooks/t/{token}", json={"event": "test"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    lookup.assert_awaited_once_with(token)


@pytest.fixture
def app_log() -> Iterator[list[str]]:
    chunks: list[str] = []
    configure_logging(level="DEBUG", fmt="text", enqueue=False, color=False, sink=chunks.append)
    yield chunks
    configure_logging()


def _app_output(chunks: list[str]) -> str:
    """Records from app modules only; the HTTP test client's own request log is not under test."""
    return "\n".join(line for line in "".join(chunks).splitlines() if " | app." in line)


def _throttled_trigger() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id=uuid.uuid4(), name="deploy-hook", config={"token": CANARY_TOKEN}, is_enabled=True
    )


@pytest.mark.parametrize(("hits", "expected"), [(60, "hard rate limit exceeded: hits=60"), (6, "per-agent rate limit")])
async def test_throttled_webhook_logs_and_audit_carry_no_token_fragment(monkeypatch, client, app_log, hits, expected):
    # Given: a rate limited webhook token whose first eight characters used to reach logs and audit details
    trigger = _throttled_trigger()
    audit, enqueue = AsyncMock(), AsyncMock()
    monkeypatch.setattr(webhooks_api.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=trigger))
    monkeypatch.setattr(
        webhooks_api.agent_dao,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=trigger.agent_id, webhook_rate_limit=5)),
    )
    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", AsyncMock(return_value=hits))
    monkeypatch.setattr(webhooks_api, "write_audit_log", audit)
    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", enqueue)

    # When: the throttled request arrives with the token in both path and query
    async with await client() as ac:
        response = await ac.post(f"/api/webhooks/t/{CANARY_TOKEN}?sig={CANARY_TOKEN}", json={"event": "test"})

    # Then: the response and throttling are unchanged and every sink is token-free but still diagnosable
    output = _app_output(app_log)
    assert response.status_code == 429
    assert response.json() == {"ok": True}
    enqueue.assert_not_awaited()
    assert expected in output
    assert "POST /api/webhooks/t/{token} 429" in output
    assert CANARY_TOKEN[:8] not in output
    if hits == 60:
        audit.assert_not_awaited()
    else:
        assert audit.await_args_list == [
            (
                ("webhook_rate_limited", {"trigger_name": "deploy-hook", "limit": 5, "trigger_id": str(trigger.id)}),
                {"agent_id": trigger.agent_id},
            )
        ]


async def test_unknown_token_stays_opaque_and_unlogged(monkeypatch, client, app_log):
    monkeypatch.setattr(webhooks_api.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=None))
    monkeypatch.setattr(
        webhooks_api, "_record_and_count_hits", AsyncMock(side_effect=AssertionError("Unknown token reached Redis"))
    )

    async with await client() as ac:
        response = await ac.post(f"/api/webhooks/t/{CANARY_TOKEN}", json={"event": "test"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "POST /api/webhooks/t/{token} 200" in _app_output(app_log)
    assert CANARY_TOKEN[:8] not in _app_output(app_log)
