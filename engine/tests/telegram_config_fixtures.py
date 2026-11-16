from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import telegram
from app.config import get_settings
from app.core.domain.agents import PermissionAccessLevel
from app.dao.channel_config_dao import channel_config_dao
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.channels import config as config_service, telegram_setup
from app.services.platform_service import platform_service

BOT_TOKEN = "123456:private_bot-token"
SECRET = "private_webhook-secret"


@dataclass
class ConfigStore:
    row: dict[str, object] | None = None
    writes: list[dict[str, object]] = field(default_factory=list)
    registrations: list[httpx.Request] = field(default_factory=list)
    registration_response: httpx.Response = field(
        default_factory=lambda: httpx.Response(200, json={"ok": True, "result": True})
    )
    registration_error: type[httpx.RequestError] | None = None
    session_depth: int = 0
    invalidate: AsyncMock = field(default_factory=AsyncMock)

    async def fetchone(self, sql: str, params: Mapping[str, object]) -> dict[str, object] | None:
        if sql.startswith("INSERT"):
            self.row = {"encrypt_key": None, **params, "extra_config": {}}
            self.writes.append(dict(self.row))
        elif sql.startswith("UPDATE"):
            assert self.row is not None
            self.row = {**self.row, **params, "extra_config": {}}
            self.writes.append(dict(self.row))
        elif sql.startswith("DELETE"):
            row, self.row = self.row, None
            return row
        return dict(self.row) if self.row is not None else None


@pytest.fixture
async def configured_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, ConfigStore, UserRecord, AgentRecord]]:
    store = ConfigStore()
    actor = UserRecord(id=uuid4())
    agent = AgentRecord(id=uuid4(), name="Telegram", creator_id=actor.id)

    @asynccontextmanager
    async def session() -> AsyncGenerator[ConfigStore]:
        store.session_depth += 1
        try:
            yield store
        finally:
            store.session_depth -= 1

    async def register(request: httpx.Request) -> httpx.Response:
        assert store.session_depth == 0
        store.registrations.append(request)
        if store.registration_error is not None:
            raise store.registration_error(
                f"provider error {request.url.raw_path!r} {request.content!r}", request=request
            )
        return store.registration_response

    monkeypatch.setattr(channel_config_dao, "session", session)
    settings = get_settings().model_copy(update={"AGENT_DATA_DIR": str(tmp_path)})
    monkeypatch.setattr(telegram, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_setup.channel_sender_link_dao, "invalidate_other_accounts", store.invalidate)
    monkeypatch.setattr(
        config_service, "check_agent_access", AsyncMock(return_value=(agent, PermissionAccessLevel.MANAGE))
    )
    monkeypatch.setattr(platform_service, "get_public_base_url", AsyncMock(return_value="https://engine.example"))
    app = FastAPI()
    app.include_router(telegram.router, prefix="/api")
    app.dependency_overrides[telegram.get_current_user] = lambda: actor
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(register)) as provider,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        monkeypatch.setattr(telegram_setup, "outbound_client", lambda: provider)
        yield client, store, actor, agent
    app.dependency_overrides.clear()
