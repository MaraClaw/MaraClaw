import hashlib
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api import webhooks as webhooks_api
from app.main import app


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def zremrangebyscore(self, _key, _minimum, _maximum):
        return None

    def zadd(self, _key, entries):
        self.zadd_entries = entries

    def zcard(self, _key):
        return None

    def expire(self, _key, _seconds):
        return None

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
    # Given: a fixed token, timestamp, and Redis pipeline
    token = "valid_token"
    now = 1_700_000_000.0
    redis = RecordingRedis()

    async def fake_get_redis():
        return redis

    monkeypatch.setattr(webhooks_api, "get_redis", fake_get_redis)
    monkeypatch.setattr(webhooks_api.time, "time", lambda: now)

    # When: recording a webhook hit
    count = await webhooks_api._record_and_count_hits(token)

    # Then: the collision-resistant member suffix uses SHA-256
    expected_member = f"{now}:{hashlib.sha256(f'{token}:{now}'.encode()).hexdigest()[:8]}"
    assert redis.pipeline_instance.zadd_entries == {expected_member: now}
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
async def test_receive_webhook_success(monkeypatch, client):
    from unittest.mock import AsyncMock

    # Setup test trigger and agent
    agent_id = uuid.uuid4()
    trigger = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="test-trigger",
        type="webhook",
        config={"token": "valid_token"},
        is_enabled=True,
    )
    agent = SimpleNamespace(id=agent_id, webhook_rate_limit=5)

    monkeypatch.setattr(webhooks_api.agent_trigger_dao, "find_webhook_by_token", AsyncMock(return_value=trigger))
    monkeypatch.setattr(webhooks_api.agent_dao, "get", AsyncMock(return_value=agent))

    # Mock redis rate limiting
    async def fake_record_and_count_hits(token):
        return 1

    monkeypatch.setattr(webhooks_api, "_record_and_count_hits", fake_record_and_count_hits)

    # Mock enqueue_webhook_execution
    async def fake_enqueue_webhook_execution(db, *, trigger, body, payload_text, payload_obj, request_headers):
        return SimpleNamespace(id=uuid.uuid4()), True

    monkeypatch.setattr(webhooks_api, "enqueue_webhook_execution", fake_enqueue_webhook_execution)

    async with await client() as ac:
        response = await ac.post("/api/webhooks/t/valid_token", json={"event": "test"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
