import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_mcp_fixtures import MCPHarness, client, mcp, mcp_db
from app.core.domain.agents import AgentStatus
from app.core.json_types import JsonObject, json_object_from
from app.dao.agent_dao import AgentDAO, agent_dao
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.dao.tenant_dao import tenant_dao
from app.dao.user_dao import UserDAO, user_dao
from app.services import agent_manager as manager_module, guest_readiness
from app.services.agent_manager import AgentManager
from app.services.agent_mcp_sync import desired_servers, sync_mcp_config
from app.services.agent_setup_lock import setup_workspace_lock
from test_guest_start_safety import Docker

__all__ = ["client", "mcp", "mcp_db"]

_SERVERS: list[JsonObject] = [
    {
        "name": "http",
        "transport": "streamable-http",
        "url": "https://mcp.test/?token=url-secret",
        "headers": {"Authorization": "Bearer header-secret"},
    },
    {"name": "sse", "transport": "sse", "url": "https://sse.test/secret", "headers": {"X-Secret": "sse-secret"}},
    {
        "name": "local",
        "transport": "stdio",
        "command": "node",
        "args": ["--token", "arg-secret"],
        "env": {"KEY": "env-secret"},
        "cwd": "/guest/workspace",
    },
]


async def test_crud_without_guest_file_is_encrypted_and_lists_only_database(mcp: MCPHarness, client: httpx.AsyncClient):
    mcp.path.unlink()
    for payload in _SERVERS:
        response = await client.post(mcp.url, json=payload)
        assert response.status_code == 201, response.text
        assert response.headers["X-MCP-Sync"] == "pending"
    rows = await mcp.db.fetchall("SELECT name,definition_ciphertext FROM agent_mcp_servers")
    assert len(rows) == 3
    ciphertext = str(rows)
    assert all(
        secret not in ciphertext
        for secret in ("url-secret", "header-secret", "sse-secret", "arg-secret", "env-secret", "node")
    )
    assert set(await desired_servers(mcp.agent.id)) == {"http", "sse", "local"}
    assert not mcp.path.exists()
    response = await client.patch(mcp.url + "/local", json={"enabled": False, "removed_env": ["KEY"]})
    assert response.status_code == 200
    assert response.headers["X-MCP-Sync"] == "pending"
    assert (await client.delete(mcp.url + "/sse")).status_code == 204
    assert [item["name"] for item in (await client.get(mcp.url)).json()["servers"]] == ["http", "local"]
    mcp.path.write_text(json.dumps({"mcp": {"servers": {"unmanaged": {"url": "https://external.test"}}}}))
    assert [item["name"] for item in (await client.get(mcp.url)).json()["servers"]] == ["http", "local"]


@pytest.mark.parametrize("mode", ["running", "resume", "create", "recreate", "migrate"])
async def test_database_restores_all_transports_on_real_lifecycle_paths(
    mcp: MCPHarness, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, mode: str
):
    for payload in _SERVERS:
        assert (await client.post(mcp.url, json=payload)).status_code == 201
    expected = mcp.servers()
    mcp.path.unlink()
    if mode == "migrate":
        mcp.root = mcp.root / "other-host"
        mcp.path.parent.mkdir(parents=True)
    manager = AgentManager.__new__(AgentManager)
    docker = Docker()
    agent = replace(mcp.agent, container_id="old", container_port=18888, status=AgentStatus.STOPPED)
    monkeypatch.setattr(manager, "docker_client", docker, raising=False)
    monkeypatch.setattr(manager, "_agent_dir", lambda _id: mcp.path.parent)
    monkeypatch.setattr(manager, "_prepare_tavily_proxy_token", AsyncMock())
    monkeypatch.setattr(manager, "_materialize_agent_dir", AsyncMock(return_value=mcp.path.parent))
    monkeypatch.setattr(manager_module.settings, "GOGCLI_ENABLED", False)
    monkeypatch.setattr(manager_module.settings, "OPENCLAW_GUEST_AGGREGATE_MEMORY_LIMIT_BYTES", 0)
    monkeypatch.setattr(guest_readiness, "READY_INTERVAL_SECONDS", 0)

    def ready(_client: object, _reference: str, _timeout: float) -> bool:
        assert mcp.servers() == expected
        return True

    monkeypatch.setattr(guest_readiness, "probe_guest", ready)
    docker.add(agent, status="running" if mode == "running" else "exited")
    monkeypatch.setattr(
        docker, "start", lambda reference: setattr(docker.inspect(reference).state, "status", "running"), raising=False
    )
    async with setup_workspace_lock(mcp.path.parent):
        if mode == "recreate":
            await manager.replace_container(agent)
        else:
            if mode in {"create", "migrate"}:
                docker.guests.clear()
                agent.container_id = None
            await manager.start_container(agent)
    assert mcp.servers() == expected
    assert mcp.path.stat().st_mode & 0o777 == 0o600
    if mode in {"running", "resume"}:
        assert docker.runs == docker.removed == []
    else:
        assert len(docker.runs) == 1


async def test_last_deletion_tombstone_survives_stale_snapshot_and_regeneration(
    mcp: MCPHarness, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    assert (await client.post(mcp.url, json=_SERVERS[0])).status_code == 201
    stale = mcp.config()
    json_object_from(json_object_from(stale["mcp"])["servers"])["unmanaged"] = {"url": "https://keep.test"}
    replace_file = Path.replace

    def fail_replace(_path: Path, _target: Path) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    response = await client.delete(mcp.url + "/http")
    assert response.status_code == 503
    assert response.json()["detail"]["saved"] is True
    monkeypatch.setattr(Path, "replace", replace_file)
    row = await mcp.db.fetchone("SELECT definition_ciphertext FROM agent_mcp_servers WHERE name='http'")
    assert row == {"definition_ciphertext": None}
    mcp.path.write_text(json.dumps(stale))
    regenerated = manager_module.preserve_guest_resources(mcp.path, {"models": {"rebuilt": True}})
    assert manager_module.agent_manager._atomic_write_json(mcp.path, regenerated)
    async with setup_workspace_lock(mcp.path.parent):
        assert await sync_mcp_config(mcp.agent.id, mcp.path)
    assert mcp.servers() == {"unmanaged": {"url": "https://keep.test"}}
    assert (await client.get(mcp.url)).json() == {"servers": []}


async def test_apply_failure_commits_desired_state_and_retry_recovers(
    mcp: MCPHarness, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    replace_file = Path.replace

    def fail(_path: Path, _target: Path) -> Path:
        raise OSError("secret-path")

    monkeypatch.setattr(Path, "replace", fail)
    response = await client.post(mcp.url, json=_SERVERS[0])
    assert response.status_code == 503
    assert response.json()["detail"]["saved"] is True
    assert mcp.servers() == {}
    assert [item["name"] for item in (await client.get(mcp.url)).json()["servers"]] == ["http"]
    assert len(await agent_mcp_server_dao.list_for_agent(mcp.agent.id)) == 1
    monkeypatch.setattr(Path, "replace", replace_file)
    response = await client.patch(mcp.url + "/http", json={"enabled": True})
    assert response.status_code == 200
    assert response.headers["X-MCP-Sync"] == "applied"
    assert json_object_from(mcp.servers()["http"])["headers"] == {"Authorization": "Bearer header-secret"}


async def test_real_database_authorization_and_agent_scope(
    mcp: MCPHarness, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(agent_dao, "get", AgentDAO.get.__get__(agent_dao))
    monkeypatch.setattr(user_dao, "get", UserDAO.get.__get__(user_dao))
    monkeypatch.setattr(user_dao, "get_with_identity", UserDAO.get_with_identity.__get__(user_dao))
    assert (await client.post(mcp.url, json=_SERVERS[0])).status_code == 201
    other = await agent_dao.create(
        obj_in={"name": "Second", "creator_id": mcp.user.id, "tenant_id": mcp.user.tenant_id}
    )
    other_url = f"/api/agents/{other.id}/mcp/servers"
    assert (await client.get(other_url)).json() == {"servers": []}
    assert (await client.patch(other_url + "/http", json={"enabled": False})).status_code == 404
    foreign = await tenant_dao.create(obj_in={"name": "Foreign", "slug": "foreign"})
    await mcp.db.execute(
        "UPDATE users SET tenant_id=%(tenant)s WHERE id=%(user)s", {"tenant": foreign.id, "user": mcp.user.id}
    )
    for method in ("GET", "POST", "PATCH", "DELETE"):
        url = mcp.url + ("/http" if method in {"PATCH", "DELETE"} else "")
        response = await client.request(method, url, json={"enabled": False} if method == "PATCH" else _SERVERS[1])
        assert response.status_code == 403, response.text
    assert len(await agent_mcp_server_dao.list_for_agent(mcp.agent.id)) == 1
