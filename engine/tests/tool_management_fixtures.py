from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import tools
from app.dao.agent_dao import agent_dao
from app.db.session import bind_crud_connection
from app.records.agent import AgentRecord
from organization_admission_fixtures import OrganizationScenario, organization_scenario

__all__ = ["organization_scenario", "tool_scenario"]


@dataclass
class ToolScenario:
    organization: OrganizationScenario
    client: AsyncClient
    agent: AgentRecord

    @property
    def headers(self) -> dict[str, str]:
        return self.organization.headers(self.organization.org_genesis)


@pytest.fixture
async def tool_scenario(organization_scenario: OrganizationScenario) -> AsyncIterator[ToolScenario]:
    actor = organization_scenario.org_genesis
    agent = await agent_dao.create(
        obj_in={"name": "Tools QA", "tenant_id": actor.tenant_id, "creator_id": actor.id, "access_mode": "private"}
    )
    app = FastAPI()
    app.include_router(tools.router, prefix="/api", dependencies=[Depends(bind_crud_connection)])
    app.include_router(tools.probe_router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://tools.test") as client:
        yield ToolScenario(organization_scenario, client, agent)
