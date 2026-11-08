import json
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.core.domain.agents import AgentStatus
from app.core.json_types import JsonObject, json_loads_object, json_object_from
from app.core.security import get_current_user
from app.dao.agent_dao import agent_dao
from app.dao.company_transfer_cleanup_dao import company_transfer_cleanup_dao
from app.dao.identity_dao import identity_dao
from app.dao.openclaw_update_dao import openclaw_update_notice_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import user_dao
from app.db.connection import DbConnection
from app.db.pool import close_pool, init_pool
from app.main import app
from app.records.agent import AgentRecord
from app.records.user import UserRecord
from app.services.agent_manager import agent_manager


@dataclass(slots=True)
class MCPHarness:
    root: Path
    user: UserRecord
    agent: AgentRecord
    db: DbConnection
    update_active: bool = False
    transfer_pending: bool = False
    gate_seen: anyio.Event = field(default_factory=anyio.Event)

    @property
    def path(self) -> Path:
        return self.root / str(self.agent.id) / "openclaw.json"

    @property
    def url(self) -> str:
        return f"/api/agents/{self.agent.id}/mcp/servers"

    def config(self) -> JsonObject:
        return json_loads_object(self.path.read_text(encoding="utf-8"))

    def servers(self) -> JsonObject:
        return json_object_from(json_object_from(self.config()["mcp"])["servers"])


@pytest.fixture
async def mcp_db() -> AsyncGenerator[DbConnection]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL required for isolated MCP PostgreSQL QA")
    schema = f"mcp_qa_{uuid4().hex}"
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        db = DbConnection(raw)
        await db.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await db.execute(f'SET search_path TO "{schema}"')
            await db.execute(await anyio.Path("scripts/schema_baseline.sql").read_text())
            await init_pool(conninfo=make_conninfo(conninfo, options=f"-csearch_path={schema}"), min_size=1, max_size=6)
            yield db
        finally:
            await close_pool()
            await db.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
async def mcp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mcp_db: DbConnection) -> MCPHarness:
    tenant = await tenant_dao.create(obj_in={"name": "MCP", "slug": "mcp"})
    identity = await identity_dao.create(obj_in={"email": "mcp@example.test", "password_hash": "not-used"})
    user = await user_dao.create(obj_in={"identity_id": identity.id, "tenant_id": tenant.id, "display_name": "MCP"})
    agent = await agent_dao.create(
        obj_in={"name": "MCP guest", "creator_id": user.id, "tenant_id": tenant.id, "status": AgentStatus.RUNNING}
    )
    state = MCPHarness(tmp_path, user, agent, mcp_db)
    state.path.parent.mkdir()
    state.path.write_text(
        json.dumps({"models": {"provider": "unchanged"}, "mcp": {"servers": {}, "other": "preserved"}}),
        encoding="utf-8",
    )

    async def current_user() -> UserRecord:
        return state.user

    async def get_user(user_id: UUID) -> UserRecord | None:
        return state.user if user_id == state.user.id else None

    async def get_with_identity(user_id: UUID, *, fresh: bool = False) -> UserRecord | None:
        return await get_user(user_id)

    async def get_agent(agent_id: UUID) -> AgentRecord | None:
        return state.agent if agent_id == state.agent.id else None

    async def gate_state(agent_id: UUID) -> tuple[bool, None]:
        state.gate_seen.set()
        return state.update_active, None

    async def transfer_pending(agent_id: UUID) -> bool:
        return state.transfer_pending

    app.dependency_overrides[get_current_user] = current_user
    monkeypatch.setattr(user_dao, "get", get_user)
    monkeypatch.setattr(user_dao, "get_with_identity", get_with_identity)
    monkeypatch.setattr(agent_dao, "get", get_agent)
    monkeypatch.setattr(openclaw_update_notice_dao, "gate_state", gate_state)
    monkeypatch.setattr(company_transfer_cleanup_dao, "is_pending", transfer_pending)
    monkeypatch.setattr(agent_manager, "_agent_dir", lambda agent_id: tmp_path / str(agent_id))
    return state


@pytest.fixture
async def client(mcp: MCPHarness) -> AsyncGenerator[httpx.AsyncClient]:
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()
