from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from tests.test_triggers_scope import TriggerScope
from tests.trigger_history_fixtures import AGENT_ID, SECRET, TRIGGER_ID, HistoryRaw

from app.api import triggers as triggers_api
from app.records.agent import AgentRecord
from app.records.trigger import AgentTriggerRecord
from app.records.user import UserRecord


@dataclass(frozen=True, slots=True)
class HistoryHTTP:
    app: FastAPI
    client: httpx.AsyncClient
    scope: TriggerScope
    raw: HistoryRaw

    @property
    def url(self) -> str:
        return f"/api/agents/{AGENT_ID}/triggers/{TRIGGER_ID}/executions"


@pytest.fixture
async def history_http(history_raw: HistoryRaw, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[HistoryHTTP]:
    tenant_id = UUID(int=3000)
    user = UserRecord(id=UUID(int=4000), tenant_id=tenant_id)
    agent = AgentRecord(id=AGENT_ID, name="History", creator_id=user.id, tenant_id=tenant_id)
    trigger = AgentTriggerRecord(id=TRIGGER_ID, agent_id=AGENT_ID, config={"token": SECRET, "secret": SECRET})
    scope = TriggerScope(user, agent, trigger, access_level="manage")
    monkeypatch.setattr(triggers_api, "check_agent_access", scope.check_access)
    monkeypatch.setattr(triggers_api, "agent_trigger_dao", scope)
    app = FastAPI()
    app.include_router(triggers_api.router)
    app.dependency_overrides[triggers_api.get_current_user] = lambda: scope.user
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://history.test") as client:
            yield HistoryHTTP(app, client, scope, history_raw)
    finally:
        app.dependency_overrides.clear()
