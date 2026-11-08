import json
from dataclasses import replace
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi import HTTPException

from agent_mcp_fixtures import MCPHarness, client, mcp, mcp_db
from app.core.domain.agents import AgentStatus
from app.core.json_types import JsonObject, json_object_from
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.services import agent_mcp_config as native
from app.services.agent_mcp_sync import encrypted_record
from app.services.agent_setup_lock import setup_workspace_lock

__all__ = ["client", "mcp", "mcp_db"]


async def test_waiting_writer_reauthorizes_after_exclusive_workspace_lock(mcp: MCPHarness, client: httpx.AsyncClient):
    responses: list[httpx.Response] = []
    before = mcp.path.read_bytes()

    async def write() -> None:
        responses.append(await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"}))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks, setup_workspace_lock(mcp.path.parent):
            tasks.start_soon(write)
            await mcp.gate_seen.wait()
            assert responses == []
            mcp.user = replace(mcp.user, is_active=False)
    assert [response.status_code for response in responses] == [401]
    assert mcp.path.read_bytes() == before


@pytest.mark.parametrize("gate", ["update", "transfer"])
async def test_exclusion_rechecked_after_waiting_for_lock(mcp: MCPHarness, client: httpx.AsyncClient, gate: str):
    responses: list[httpx.Response] = []
    before = mcp.path.read_bytes()

    async def write() -> None:
        responses.append(await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"}))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks, setup_workspace_lock(mcp.path.parent):
            tasks.start_soon(write)
            await mcp.gate_seen.wait()
            assert responses == []
            match gate:
                case "update":
                    mcp.update_active = True
                case "transfer":
                    mcp.transfer_pending = True
    assert [response.status_code for response in responses] == [409 if gate == "update" else 423]
    assert mcp.path.read_bytes() == before


async def test_concurrent_creates_preserve_both_servers(mcp: MCPHarness, client: httpx.AsyncClient):
    responses: list[httpx.Response] = []

    async def write(name: str) -> None:
        responses.append(await client.post(mcp.url, json={"name": name, "transport": "sse", "url": "http://mcp"}))

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(write, "first")
            tasks.start_soon(write, "second")
    assert [response.status_code for response in responses] == [201, 201]
    assert set(mcp.servers()) == {"first", "second"}


@pytest.mark.parametrize("mode", ["off", "restart"])
async def test_running_gateway_without_hot_reload_blocks_writes_but_allows_inventory(
    mcp: MCPHarness, client: httpx.AsyncClient, mode: str
):
    config = mcp.config()
    config["gateway"] = {"reload": {"mode": mode}}
    mcp.path.write_text(json.dumps(config), encoding="utf-8")
    before = mcp.path.read_bytes()
    assert (await client.get(mcp.url)).status_code == 200
    response = await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"})
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "mcp_saved_sync_pending",
        "saved": True,
        "sync_error": "mcp_hot_reload_required",
    }
    assert (await client.get(mcp.url)).json()["servers"][0]["name"] == "test"
    assert mcp.path.read_bytes() == before
    mcp.agent = replace(mcp.agent, status=AgentStatus.STOPPED)
    assert (await client.patch(mcp.url + "/test", json={"enabled": True})).status_code == 200


async def test_failed_atomic_replace_preserves_original_and_cleans_staging(
    mcp: MCPHarness, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    before = mcp.path.read_bytes()

    def fail_replace(path: Path, target: Path) -> Path:
        raise OSError("private-path-secret")

    monkeypatch.setattr(Path, "replace", fail_replace)
    response = await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"})
    assert response.status_code == 503
    assert "private-path-secret" not in response.text
    assert mcp.path.read_bytes() == before
    assert list(mcp.path.parent.glob(".mcp-*")) == []


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"headers": {"X": "secret"}, "removed_headers": ["x"]},
        {"url": None},
        {"transport": "stdio"},
        {"env": {"KEY": "secret"}},
        {"env": {}},
        {"args": []},
    ],
)
async def test_invalid_patch_never_changes_native_config(mcp: MCPHarness, client: httpx.AsyncClient, patch: JsonObject):
    created = await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"})
    assert created.status_code == 201
    before = mcp.path.read_bytes()
    response = await client.patch(mcp.url + "/test", json=patch)
    assert response.status_code == 422, response.text
    assert "secret" not in response.text
    assert mcp.path.read_bytes() == before


async def test_legacy_native_transport_inference_and_extra_fields_survive_patch(
    mcp: MCPHarness, client: httpx.AsyncClient
):
    config: JsonObject = {
        "mcp": {"servers": {"legacy": {"url": "http://mcp", "auth": "oauth", "oauth": {"scope": "secret"}}}}
    }
    mcp.path.write_text(json.dumps(config), encoding="utf-8")
    legacy = json_object_from(json_object_from(json_object_from(config["mcp"])["servers"])["legacy"])
    await agent_mcp_server_dao.save(encrypted_record(mcp.agent.id, "legacy", legacy))
    response = await client.get(mcp.url)
    assert response.status_code == 200
    assert response.json()["servers"][0]["transport"] == "sse"
    assert "secret" not in response.text
    assert (await client.patch(mcp.url + "/legacy", json={"enabled": False})).status_code == 200
    assert json_object_from(mcp.servers()["legacy"])["oauth"] == {"scope": "secret"}


def test_config_parser_requires_exact_json_object(tmp_path: Path):
    path = tmp_path / "openclaw.json"
    path.write_text('{"mcp":{"servers":{}},"unexpected":NaN}', encoding="utf-8")
    with pytest.raises(HTTPException) as error:
        native.read_config(path)
    assert error.value.status_code == 409
