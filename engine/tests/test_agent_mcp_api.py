from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from agent_mcp_fixtures import MCPHarness, client, mcp, mcp_db
from app.core.json_types import JsonObject, json_object_from
from app.dao.agent_mcp_dao import agent_mcp_server_dao
from app.services.agent_manager import agent_manager
from app.services.agent_mcp_sync import encrypted_record
from app.services.guest_resource_config import preserve_guest_resources

__all__ = ["client", "mcp", "mcp_db"]


async def test_remote_crud_preserves_native_config_and_hides_sensitive_values(
    mcp: MCPHarness, client: httpx.AsyncClient
):
    before_owner = mcp.path.stat()
    response = await client.post(
        mcp.url,
        json={
            "name": "search",
            "transport": "streamable-http",
            "url": "https://user:url-secret@example.com/private-token/mcp?token=query-secret",
            "headers": {"Authorization": "Bearer header-secret", "X-Retain": "retained-secret"},
            "connection_timeout_ms": 1000.5,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["header_names"] == ["Authorization", "X-Retain"]
    assert response.json()["url_configured"] is True
    for secret in ("url-secret", "private-token", "query-secret", "header-secret", "retained-secret"):
        assert secret not in response.text
    stored = json_object_from(mcp.servers()["search"])
    assert stored["transport"] == "streamable-http"
    assert stored["connectionTimeoutMs"] == 1000.5
    assert stored["url"] == "https://user:url-secret@example.com/private-token/mcp?token=query-secret"
    assert mcp.path.stat().st_mode & 0o777 == 0o600
    assert (mcp.path.stat().st_uid, mcp.path.stat().st_gid) == (before_owner.st_uid, before_owner.st_gid)
    assert list(mcp.path.parent.glob(".mcp-*")) == []
    mcp.path.chmod(0o644)
    assert (await client.patch(mcp.url + "/search", json={"enabled": True})).status_code == 200
    assert mcp.path.stat().st_mode & 0o777 == 0o600
    assert (
        await client.post(mcp.url, json={"name": "search", "transport": "sse", "url": "http://mcp"})
    ).status_code == 409
    other = await client.post(mcp.url, json={"name": "other", "transport": "sse", "url": "http://mcp"})
    assert other.status_code == 201
    preserved_other = mcp.servers()["other"]
    response = await client.patch(
        mcp.url + "/search",
        json={"headers": {"authorization": "rotated-secret"}, "removed_headers": ["x-retain"], "enabled": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False
    stored = json_object_from(mcp.servers()["search"])
    assert stored["headers"] == {"authorization": "rotated-secret"}
    assert stored["url"] == "https://user:url-secret@example.com/private-token/mcp?token=query-secret"
    inventory = await client.get(mcp.url)
    assert inventory.status_code == 200
    assert [item["name"] for item in inventory.json()["servers"]] == ["other", "search"]
    assert "rotated-secret" not in inventory.text
    assert (await client.delete(mcp.url + "/search")).status_code == 204
    assert mcp.servers() == {"other": preserved_other}
    assert mcp.config()["models"] == {"provider": "unchanged"}
    assert json_object_from(mcp.config()["mcp"])["other"] == "preserved"
    regenerated = preserve_guest_resources(mcp.path, {"models": {"refreshed": True}})
    assert json_object_from(regenerated["mcp"])["servers"] == {"other": preserved_other}
    assert agent_manager._atomic_write_json(mcp.path, regenerated)
    assert mcp.path.stat().st_mode & 0o777 == 0o600
    assert (mcp.path.stat().st_uid, mcp.path.stat().st_gid) == (before_owner.st_uid, before_owner.st_gid)
    assert (await client.delete(mcp.url + "/search")).status_code == 404


async def test_stdio_patch_retains_secrets_and_native_extra_fields(mcp: MCPHarness, client: httpx.AsyncClient):
    response = await client.post(
        mcp.url,
        json={
            "name": "local",
            "transport": "stdio",
            "command": "node",
            "args": ["/guest/server.mjs", "--api-key", "argv-secret"],
            "env": {"API_KEY": "env-secret", "KEEP": "keep-secret"},
            "cwd": "/home/node/workspace",
            "request_timeout_ms": 20000,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["args_count"] == 3
    assert "argv-secret" not in response.text
    assert "env-secret" not in response.text
    server = json_object_from(mcp.servers()["local"])
    server["toolFilter"] = ["allowed"]
    await agent_mcp_server_dao.save(encrypted_record(mcp.agent.id, "local", server))
    response = await client.patch(mcp.url + "/local", json={"enabled": False, "env": {"NEW": True}})
    assert response.status_code == 200, response.text
    stored = json_object_from(mcp.servers()["local"])
    assert stored["env"] == {"API_KEY": "env-secret", "KEEP": "keep-secret", "NEW": True}
    assert stored["args"] == ["/guest/server.mjs", "--api-key", "argv-secret"]
    assert stored["toolFilter"] == ["allowed"]
    response = await client.patch(
        mcp.url + "/local", json={"args": [], "removed_env": ["API_KEY"], "cwd": None, "request_timeout_ms": None}
    )
    assert response.status_code == 200, response.text
    stored = json_object_from(mcp.servers()["local"])
    assert stored["env"] == {"KEEP": "keep-secret", "NEW": True}
    assert stored["args"] == []
    assert "cwd" not in stored
    assert "requestTimeoutMs" not in stored


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "../outside", "transport": "sse", "url": "https://host"},
        {"name": "__proto__", "transport": "sse", "url": "https://host"},
        {"name": "bad", "transport": "sse", "url": "file:///engine/secret"},
        {"name": "bad", "transport": "sse", "url": "https://host", "command": "sh"},
        {"name": "bad", "transport": "stdio", "command": " "},
        {"name": "bad", "transport": "stdio", "command": "sh", "args": [False]},
        {"name": "bad", "transport": "stdio", "command": "sh", "env": {"X": {"secret": "body-secret"}}},
        {"name": "bad", "transport": "sse", "url": "https://host", "headers": {"X": "body-secret\n"}},
        {"name": "bad", "transport": "sse", "url": "https://host", "request_timeout_ms": 0},
        {"name": "bad", "transport": "sse", "url": "https://host", "enabled": "body-secret"},
        {"name": "bad", "transport": "sse", "url": "https://host", "unknown": "body-secret"},
        {"name": "bad", "transport": "sse", "url": "https://host", "args": []},
        {"name": "bad", "transport": "sse", "url": "https://host", "env": {}},
        {"name": "bad", "transport": "stdio", "command": "node", "headers": {}},
        {"name": "bad", "transport": "stdio", "command": "node", "env": {"body-secret": {"invalid": True}}},
    ],
)
async def test_bad_input_is_secret_safe_and_does_not_write(
    mcp: MCPHarness, client: httpx.AsyncClient, payload: JsonObject
):
    before = mcp.path.read_bytes()
    response = await client.post(mcp.url, json=payload)
    assert response.status_code == 422
    assert "body-secret" not in response.text
    assert mcp.path.read_bytes() == before


@pytest.mark.parametrize("access", ["use", "foreign", "inactive"])
@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
async def test_manager_only_with_real_fresh_authorization(
    mcp: MCPHarness, client: httpx.AsyncClient, access: str, method: str
):
    match access:
        case "use":
            mcp.agent = replace(mcp.agent, creator_id=uuid4())
        case "foreign":
            mcp.user = replace(mcp.user, tenant_id=uuid4())
        case "inactive":
            mcp.user = replace(mcp.user, is_active=False)
    before = mcp.path.read_bytes()
    url = mcp.url + ("/target" if method in {"PATCH", "DELETE"} else "")
    body: JsonObject = (
        {"enabled": False} if method == "PATCH" else {"name": "test", "transport": "sse", "url": "http://mcp"}
    )
    response = await client.request(method, url, json=body)
    assert response.status_code == (401 if access == "inactive" else 403), response.text
    assert mcp.path.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    ["{", "[]", '{"mcp":[]}', '{"mcp":{"servers":{"bad":null}}}', '{"mcp":{"servers":{"bad":{"transport":"stdio"}}}}'],
)
async def test_invalid_native_config_is_never_overwritten(mcp: MCPHarness, client: httpx.AsyncClient, content: str):
    mcp.path.write_text(content, encoding="utf-8")
    response = await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"})
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["saved"] is True
    assert mcp.path.read_text(encoding="utf-8") == content


async def test_missing_and_unreadable_config_never_creates_or_replaces_it(mcp: MCPHarness, client: httpx.AsyncClient):
    mcp.path.unlink()
    assert (await client.get(mcp.url)).json() == {"servers": []}
    assert not mcp.path.exists()
    mcp.path.mkdir()
    response = await client.post(mcp.url, json={"name": "test", "transport": "sse", "url": "http://mcp"})
    assert response.status_code == 503
    assert mcp.path.is_dir()
