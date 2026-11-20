import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.config import get_settings
from app.services import file_share_tokens as shares

TTL_SECONDS = 900
PATH = "workspace/report.docx"


class ShareRedis:
    def __init__(self):
        self.values = {}
        self.now = 0

    async def set(self, key, value, *, nx, ex):
        assert nx
        stored = self.values.get(key)
        if stored and stored[1] > self.now:
            return None
        self.values[key] = (value, self.now + ex)
        return True

    async def get(self, key):
        value = self.values.get(key)
        return value[0] if value and value[1] > self.now else None


@pytest.fixture
def redis(monkeypatch):
    fake = ShareRedis()
    monkeypatch.setattr(shares, "get_redis", AsyncMock(return_value=fake))
    return fake


async def test_issued_token_resolves_until_its_ttl_without_a_download_cap(redis):
    agent_id = uuid.uuid4()
    before = datetime.now(UTC)
    token, expires_at = await shares.issue(agent_id, PATH, TTL_SECONDS)
    after = datetime.now(UTC)
    assert before + timedelta(seconds=TTL_SECONDS) <= expires_at <= after + timedelta(seconds=TTL_SECONDS)
    grant = shares.FileShareGrant(agent_id=agent_id, path=PATH)
    assert [await shares.resolve(token) for _ in range(3)] == [grant] * 3


async def test_unknown_token_resolves_to_none(redis):
    assert await shares.resolve("never-issued") is None


async def test_token_expires_with_its_redis_ttl(redis):
    token, _ = await shares.issue(uuid.uuid4(), PATH, TTL_SECONDS)
    redis.now = TTL_SECONDS - 1
    assert await shares.resolve(token) is not None
    redis.now = TTL_SECONDS
    assert await shares.resolve(token) is None


async def test_redis_holds_only_the_token_hash(redis):
    agent_id = uuid.uuid4()
    token, _ = await shares.issue(agent_id, PATH, TTL_SECONDS)
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = f"{get_settings().REDIS_KEY_PREFIX}:file-share:{digest}"
    assert list(redis.values) == [key]
    value = redis.values[key][0]
    assert token not in key
    assert token not in value
    assert json.loads(value) == {"agent_id": str(agent_id), "path": PATH}


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{}",
        '{"agent_id": "not-a-uuid", "path": "workspace/a.txt"}',
        f'{{"agent_id": "{uuid.uuid4()}", "path": 3}}',
    ],
)
async def test_unreadable_payload_resolves_to_none(redis, payload):
    redis.values[shares.token_key("forged")] = (payload, 1000)
    assert await shares.resolve("forged") is None


async def test_issue_retries_a_token_collision(redis, monkeypatch):
    redis.values[shares.token_key("taken")] = ("{}", 1000)
    candidates = iter(["taken", "fresh"])
    monkeypatch.setattr(shares, "secrets", SimpleNamespace(token_urlsafe=lambda _: next(candidates)))
    agent_id = uuid.uuid4()
    token, _ = await shares.issue(agent_id, PATH, TTL_SECONDS)
    assert token == "fresh"
    assert await shares.resolve("fresh") == shares.FileShareGrant(agent_id=agent_id, path=PATH)
    assert redis.values[shares.token_key("taken")] == ("{}", 1000)


async def test_issue_gives_up_after_bounded_collisions(redis, monkeypatch):
    redis.values[shares.token_key("taken")] = ("{}", 1000)
    attempts = []

    def always_taken(_):
        attempts.append(1)
        return "taken"

    monkeypatch.setattr(shares, "secrets", SimpleNamespace(token_urlsafe=always_taken))
    with pytest.raises(shares.FileShareStoreUnavailableError):
        await shares.issue(uuid.uuid4(), PATH, TTL_SECONDS)
    assert len(attempts) == shares.ISSUE_ATTEMPTS
    assert redis.values == {shares.token_key("taken"): ("{}", 1000)}


@pytest.mark.parametrize("operation", ["issue", "resolve"])
@pytest.mark.parametrize("failure", ["connect", "command"])
async def test_redis_failure_raises_store_unavailable(redis, monkeypatch, operation, failure):
    if failure == "connect":
        monkeypatch.setattr(shares, "get_redis", AsyncMock(side_effect=RedisError()))
    else:
        monkeypatch.setattr(redis, "set", AsyncMock(side_effect=RedisError()))
        monkeypatch.setattr(redis, "get", AsyncMock(side_effect=RedisError()))
    with pytest.raises(shares.FileShareStoreUnavailableError):
        await (shares.issue(uuid.uuid4(), PATH, TTL_SECONDS) if operation == "issue" else shares.resolve("token"))
