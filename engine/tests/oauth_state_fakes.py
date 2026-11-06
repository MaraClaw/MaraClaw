"""In-memory Redis for the single-use SSO state and pending-token tests."""

from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError

from app.services import oauth_state
from app.services.auth_registry import auth_provider_registry

REDIRECT_URI = "https://app.example.test/sso/callback"


class StateRedis:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail_on: set[str] = set()

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if "set" in self.fail_on:
            raise RedisError("unavailable")
        if nx and key in self.rows:
            return False
        self.rows[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def getdel(self, key: str) -> str | None:
        if "getdel" in self.fail_on:
            raise RedisError("unavailable")
        self.ttls.pop(key, None)
        return self.rows.pop(key, None)

    def expire_all(self) -> None:
        self.rows.clear()
        self.ttls.clear()


def install_state_redis(monkeypatch: pytest.MonkeyPatch) -> StateRedis:
    redis = StateRedis()
    getter = AsyncMock(return_value=redis)
    monkeypatch.setattr(oauth_state, "get_redis", getter)
    monkeypatch.setattr("app.core.events.get_redis", getter)
    return redis


async def sign_in_state(provider: str, redirect_uri: str = REDIRECT_URI) -> str:
    return await oauth_state.issue(provider, redirect_uri, oauth_state.OAuthIntent.SIGN_IN)


async def exchange_mock(provider_type: str) -> AsyncMock:
    provider = await auth_provider_registry.get_provider(provider_type)
    assert provider is not None
    exchange = provider.exchange_code_for_token
    assert isinstance(exchange, AsyncMock)
    return exchange
