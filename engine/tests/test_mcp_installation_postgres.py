"""Opt-in real persistence tests: BOOTSTRAP_TEST_DATABASE_URL owns a disposable schema."""

import json
import os
import re
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import DictRow, dict_row

from app.core.security import decrypt_data
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.db.connection import DbConnection
from app.db.errors import ForeignKeyViolationError
from app.db.pool import close_pool, init_pool
from app.db.session import connection_ctx
from app.services import resource_discovery
from app.services.agent_tool_exec.mcp_tools import _execute_mcp_tool
from app.services.tool_config import get_settings


@pytest.fixture
async def installation_db() -> AsyncIterator[tuple[UUID, UUID, str]]:
    conninfo = os.environ.get("BOOTSTRAP_TEST_DATABASE_URL", "")
    if not conninfo:
        pytest.skip("BOOTSTRAP_TEST_DATABASE_URL is required for isolated PostgreSQL QA")
    schema = f"mcp_install_{uuid4().hex}"
    isolated = make_conninfo(conninfo, options=f"-csearch_path={schema}")
    agents = (uuid4(), uuid4())
    async with await AsyncConnection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as raw:
        await raw.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            await raw.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            # Real tool DDL, with only its unrelated agent aggregate replaced by an FK target.
            await raw.execute("CREATE TABLE agents (id UUID PRIMARY KEY)")
            baseline = await anyio.Path("scripts/schema_baseline.sql").read_text()
            for table in ("tools", "agent_tools"):
                definition = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\n\);", baseline, re.DOTALL)
                assert definition is not None
                await DbConnection(raw).execute(definition.group())
            for agent in agents:
                await raw.execute("INSERT INTO agents (id) VALUES (%s)", (agent,))
            await init_pool(conninfo=isolated, min_size=1, max_size=2)
            yield *agents, isolated
        finally:
            await close_pool()
            await raw.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def mcp_wire(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/broken":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload.get("method")
        result = {"content": [{"type": "text", "text": "pong"}]} if method == "tools/call" else {}
        if method == "tools/list":
            result = {"tools": [{"name": "ping", "inputSchema": {"type": "object", "properties": {}}}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload.get("id", 1), "result": result})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return requests


async def test_failed_discovery_saves_only_a_disabled_placeholder(installation_db, mcp_wire) -> None:
    # Given an endpoint that rejects both supported transports.
    agent, _, _ = installation_db
    # When import saves its retryable configuration.
    result = await resource_discovery.import_mcp_direct("https://8.8.8.8/broken", agent, server_name="broken")
    # Then it cannot enter the callable catalog or claim readiness.
    tool = await tool_dao.get_by_name("mcp_broken")
    assert tool is not None
    assert tool.enabled is False
    assert tool.mcp_tool_name is None
    assert "now available" not in result
    assert "not ready" in result.lower()


@pytest.mark.parametrize("in_query", [False, True])
async def test_imported_credentials_remain_callable_after_pool_restart(installation_db, mcp_wire, in_query) -> None:
    # Given separate agents importing the same remote tool with different secrets.
    agent, other, isolated = installation_db
    key = "agent-a-key&with=symbols"
    url = "https://8.8.8.8/mcp?apiKey=agent-a-key%26with%3Dsymbols" if in_query else "https://8.8.8.8/mcp"
    await resource_discovery.import_mcp_direct(url, agent, server_name="echo", api_key=None if in_query else key)
    await resource_discovery.import_mcp_direct("https://8.8.8.8/mcp", other, server_name="echo", api_key="agent-b-key")
    tool = await tool_dao.get_by_name("mcp_echo_ping")
    assert tool is not None
    assignment = await agent_tool_dao.get_assignment(agent, tool.id)
    assert assignment is not None
    assert decrypt_data(assignment.config["api_key"], get_settings().SECRET_KEY) == key
    assert key not in str(tool)
    assert "apiKey" not in (tool.mcp_server_url or "")
    # When a fresh process-equivalent pool and fresh MCP client execute the installed tool.
    await close_pool()
    await init_pool(conninfo=isolated, min_size=1, max_size=2)
    result = await _execute_mcp_tool(tool.name, {}, agent)
    other_result = await _execute_mcp_tool(tool.name, {}, other)
    # Then credentials are recovered from each agent's encrypted assignment, not memory or guest config.
    assert result == other_result == "pong"
    calls = [r for r in mcp_wire if r.method == "POST" and json.loads(r.content).get("method") == "tools/call"]
    assert [r.headers["authorization"] for r in calls] == [f"Bearer {key}", "Bearer agent-b-key"]


async def test_assignment_failure_rolls_back_tool_metadata(installation_db, mcp_wire) -> None:
    # Given successful discovery but an agent that no longer exists.
    # When its foreign-key failure interrupts persistence.
    with pytest.raises(ForeignKeyViolationError):
        await resource_discovery.import_mcp_direct("https://8.8.8.8/mcp", uuid4(), server_name="rollback")
    # Then no global orphan survives the unsuccessful installation.
    assert await tool_dao.get_by_name("mcp_rollback_ping") is None


async def test_assignment_failure_uses_savepoint_inside_caller_transaction(installation_db, mcp_wire) -> None:
    # Given an outer transaction whose caller handles an install failure.
    async with connection_ctx() as db:
        await db.execute("INSERT INTO agents (id) VALUES (%(id)s)", {"id": uuid4()})
        # When installation fails after metadata creation.
        with pytest.raises(ForeignKeyViolationError):
            await resource_discovery.import_mcp_direct("https://8.8.8.8/mcp", uuid4(), server_name="nested")
        # Then its savepoint is rolled back without poisoning the caller's transaction.
        assert await tool_dao.get_by_name("mcp_nested_ping") is None


async def test_smithery_promotion_preserves_other_agents(installation_db, monkeypatch) -> None:
    # Given a generic server configured independently for two agents.
    agent, other, _ = installation_db
    generic = await tool_dao.create(obj_in={"name": "mcp_vendor_echo", "type": "mcp", "source": "agent"})
    await agent_tool_dao.ensure_with_config(agent, generic.id, config={"marker": "a"})
    await agent_tool_dao.ensure_with_config(other, generic.id, config={"marker": "b"})

    async def key(_agent: UUID) -> str:
        return "smithery-key"

    async def connect(*_args):
        return {"namespace": "qa", "connection_id": "echo"}

    async def send(_transport, request):
        if request.method == "POST":
            return httpx.Response(200, json={"result": {"tools": [{"name": "ping", "inputSchema": {}}]}})
        if request.url.path.endswith("/servers"):
            return httpx.Response(
                200, json={"servers": [{"qualifiedName": "vendor/echo", "displayName": "Echo", "remote": True}]}
            )
        return httpx.Response(200, json={"deploymentUrl": "https://vendor.run.tools", "tools": [{"name": "ping"}]})

    monkeypatch.setattr(resource_discovery, "_get_smithery_api_key", key)
    monkeypatch.setattr(resource_discovery, "_ensure_smithery_connection", connect)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    # When one agent upgrades the placeholder to discovered tools.
    await resource_discovery.import_mcp_from_smithery("vendor/echo", agent, reauthorize=True)
    # Then another agent's assignment/configuration is not deleted or reassigned.
    survivor = await agent_tool_dao.get_assignment(other, generic.id)
    assert survivor is not None
    assert survivor.config == {"marker": "b"}
