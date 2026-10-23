import base64
import json
import uuid
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from redis.exceptions import RedisError

from app.core.json_types import JsonObject
from app.records.identity import IdentityRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.services import chatgpt_oauth, chatgpt_subscription_handoff as browser, chatgpt_subscription_store as store
from app.services.chatgpt_subscription_types import ChatGPTSubscriptionCompleteIn, ChatGPTSubscriptionStartOut

_NOW = datetime.now(UTC)
_TENANT = uuid.uuid4()
_OTHER = uuid.uuid4()


def _jwt(payload: JsonObject) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


_ACCESS = _jwt({"chatgpt_account_id": "acct-live", "sub": "user-1"})
_REFRESHED = _jwt({"chatgpt_account_id": "acct-live", "sub": "user-1", "iat": 2})


def _user(*, role: str = "org_admin", tenant_id: uuid.UUID | None = _TENANT) -> UserRecord:
    identity = IdentityRecord(id=uuid.uuid4(), email="admin@example.test")
    return UserRecord(id=uuid.uuid4(), role=role, tenant_id=tenant_id, identity_id=identity.id, identity=identity)


def _model(**kwargs: Any) -> LLMModelRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "provider": "openai",
        "model": "gpt-5.4",
        "api_key_encrypted": "enc-secret-key-9999",
        "label": "ChatGPT",
        "tenant_id": _TENANT,
        "base_url": "https://chatgpt.com/backend-api/codex",
        "max_tokens_per_day": None,
        "enabled": True,
        "supports_vision": True,
        "temperature": None,
        "request_timeout": None,
        "max_output_tokens": None,
        "created_at": _NOW,
        "updated_at": _NOW,
        "auth_kind": "chatgpt_subscription",
        "refresh_token_encrypted": "enc-refresh",
        "token_expires_at": _NOW + timedelta(hours=1),
    }
    defaults.update(kwargs)
    return LLMModelRecord(**defaults)


class FakeChatGPTTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.token_status: int = 200
        self.token_body: JsonObject = {
            "access_token": _ACCESS,
            "refresh_token": "oa-refresh-LIVE",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid profile email offline_access",
            "id_token": _ACCESS,
        }

    async def post_form(self, url: str, data: dict[str, str]) -> tuple[int, JsonObject]:
        self.calls.append((url, "form", dict(data)))
        if url == chatgpt_oauth.OPENAI_TOKEN_URL:
            if data.get("grant_type") == "refresh_token":
                return 200, {**self.token_body, "access_token": _REFRESHED}
            return self.token_status, self.token_body
        return 404, {"error": "not_found"}


class Clock:
    def __init__(self) -> None:
        self.current: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def now(self, _tz: tzinfo) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class TicketRedis:
    def __init__(self) -> None:
        self.clock: Clock = Clock()
        self.rows: dict[str, tuple[str, datetime]] = {}
        self.writes: list[tuple[str, int, bool]] = []
        self.fail_on: set[str] = set()

    def _get(self, key: str) -> str | None:
        row = self.rows.get(key)
        if row is None:
            return None
        value, expiry = row
        if expiry <= self.clock.current:
            self.rows.pop(key)
            return None
        return value

    async def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
        if "set" in self.fail_on:
            raise RedisError("unavailable")
        if nx and self._get(key) is not None:
            return False
        self.rows[key] = value, self.clock.current + timedelta(seconds=ex)
        self.writes.append((key, ex, nx))
        return True

    async def get(self, key: str) -> str | None:
        if "get" in self.fail_on:
            raise RedisError("unavailable")
        return self._get(key)

    async def getdel(self, key: str) -> str | None:
        if "getdel" in self.fail_on:
            raise RedisError("unavailable")
        value = self._get(key)
        self.rows.pop(key, None)
        return value


@pytest.fixture
def oauth_store(monkeypatch):
    redis = TicketRedis()
    monkeypatch.setattr(store, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(store, "datetime", redis.clock)
    monkeypatch.setattr(browser, "datetime", redis.clock)
    monkeypatch.setattr(browser.llm_model_dao, "get_subscription_for_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr(chatgpt_oauth, "_transport", None)
    return redis


def callback(start: ChatGPTSubscriptionStartOut, **params: str) -> ChatGPTSubscriptionCompleteIn:
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    query = {"state": state, "code": "authorization-code"} if not params else {"state": state, **params}
    return ChatGPTSubscriptionCompleteIn(
        attempt_id=start.attempt_id, callback_url=f"http://localhost:1455/auth/callback?{urlencode(query)}"
    )
