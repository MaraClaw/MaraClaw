import importlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.agent_tool_exec import _agent_tool_exec_deploy as deploy_mod
from app.services.agent_tools import (
    _check_neon_quota_limit,
    _get_vercel_quota_summary,
    _get_vercel_token,
    _neon_create_database,
    _vercel_deploy,
    _vercel_get_deploy_logs,
    _vercel_list_deployments,
    _vercel_manage_domain,
    _vercel_set_env,
)


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_tool_config")
async def test_get_vercel_token(mock_get_config):
    # Case 1: Direct configuration present
    mock_get_config.return_value = {"vercel_token": "my-direct-token"}
    token = await _get_vercel_token(uuid.uuid4(), "vercel_list_deployments")
    assert token == "my-direct-token"

    # Case 2: Config is missing, fallback to vercel_deploy tool configuration
    mock_get_config.side_effect = lambda agent_id, tool_name: (
        None if tool_name == "vercel_list_deployments" else {"vercel_token": "fallback-token"}
    )
    token = await _get_vercel_token(uuid.uuid4(), "vercel_list_deployments")
    assert token == "fallback-token"


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_check_neon_quota_limit(mock_get):
    # Case 1: Quota reached (1 project)
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"projects": [{"id": "proj_1", "name": "my-existing-db"}]}
    )
    is_blocked, msg = await _check_neon_quota_limit("test-key")
    assert is_blocked is True
    assert "Neon Free Tier Limit Reached" in msg

    # Case 2: Quota not reached (0 projects)
    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"projects": []})
    is_blocked, msg = await _check_neon_quota_limit("test-key")
    assert is_blocked is False
    assert "0/1" in msg


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
@patch("httpx.AsyncClient.patch")
@patch("httpx.AsyncClient.post")
@patch("httpx.AsyncClient.get")
async def test_vercel_deploy_github(mock_get, mock_post, mock_patch, mock_get_token, tmp_path):
    mock_get_token.return_value = "fake-token"

    # Mock project protection patch
    mock_patch.return_value = MagicMock(status_code=200, json=dict)

    # Mock project linking and trigger
    mock_post.side_effect = [
        MagicMock(status_code=200, json=dict),  # Link repo
        MagicMock(status_code=200, json=lambda: {"id": "dep_123", "url": "test.vercel.app"}),  # Trigger deployment
    ]

    # Mock polling status to return READY immediately
    mock_get.side_effect = [
        MagicMock(status_code=200, json=lambda: {"id": "proj_123", "name": "my-project"}),  # Project check GET
        MagicMock(
            status_code=200, json=lambda: {"readyState": "READY", "url": "test.vercel.app"}
        ),  # Deployment info GET
        MagicMock(status_code=200, json=lambda: {"projects": []}),  # Project list for quota
        MagicMock(
            status_code=200, json=lambda: {"user": {"username": "test_user", "billing": {"plan": "Hobby"}}}
        ),  # User billing
    ]

    result = await _vercel_deploy(
        agent_id=uuid.uuid4(),
        ws=tmp_path,
        arguments={
            "project_name": "my-project",
            "deploy_method": "github",
            "github_repo": "owner/repo",
            "production": True,
        },
    )
    assert "Deployment triggered successfully" in result
    assert "test.vercel.app" in result


@pytest.mark.asyncio
async def test_deploy_polling_uses_facade_asyncio_without_live_sleep(monkeypatch, tmp_path):
    import httpx

    from app.services import agent_tools

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    sleep_delays: list[float] = []
    monkeypatch.setattr(agent_tools, "_get_vercel_token", AsyncMock(return_value="fake-token"))
    monkeypatch.setattr(deploy_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(httpx.AsyncClient, "patch", AsyncMock(return_value=MagicMock(status_code=200)))
    monkeypatch.setattr(
        httpx.AsyncClient,
        "post",
        AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=dict),
                MagicMock(status_code=200, json=lambda: {"id": "dep_123", "url": "test.vercel.app"}),
            ]
        ),
    )
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=lambda: {"id": "proj_123"}),
                MagicMock(status_code=200, json=lambda: {"readyState": "QUEUED", "url": "test.vercel.app"}),
                MagicMock(status_code=200, json=lambda: {"readyState": "READY", "url": "test.vercel.app"}),
                MagicMock(status_code=200, json=lambda: {"projects": []}),
                MagicMock(
                    status_code=200, json=lambda: {"user": {"username": "test_user", "billing": {"plan": "Hobby"}}}
                ),
            ]
        ),
    )

    result = await _vercel_deploy(
        agent_id=uuid.uuid4(),
        ws=tmp_path,
        arguments={"project_name": "my-project", "deploy_method": "github", "github_repo": "owner/repo"},
    )

    assert sleep_delays == [2.0]
    assert result.startswith("✅")


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
async def test_vercel_deploy_missing_token_with_existing_source_dir(mock_get_token, tmp_path):
    mock_get_token.return_value = None
    source_dir = tmp_path / "site"
    source_dir.mkdir()

    result = await _vercel_deploy(
        agent_id=uuid.uuid4(),
        ws=tmp_path,
        arguments={"project_name": "my-project", "source_dir": "site"},
    )

    assert result == "❌ Vercel Access Token is not configured. Please paste your token in the tool settings."


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
@patch("httpx.AsyncClient.get")
async def test_vercel_list_deployments_formats_integer_millisecond_timestamp(mock_get, mock_get_token):
    mock_get_token.return_value = "fake-token"
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: {
            "deployments": [
                {
                    "url": "my-project-git-main.vercel.app",
                    "state": "READY",
                    "created": 1700000000000,
                    "uid": "dep_123",
                }
            ]
        },
    )

    result = await _vercel_list_deployments(
        agent_id=uuid.uuid4(),
        arguments={"project_name": "my-project"},
    )

    assert "📋 **Deployments for my-project**:" in result
    assert "Created: 2023-11-14 22:13:20 UTC" in result
    assert "ID: `dep_123`" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
@patch("httpx.AsyncClient.get")
async def test_vercel_get_deploy_logs_strips_url_prefixed_deployment_id(mock_get, mock_get_token):
    mock_get_token.return_value = "fake-token"
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [
            {"payload": {"text": "  build started  "}},
            {"text": "build finished"},
        ],
    )

    result = await _vercel_get_deploy_logs(
        agent_id=uuid.uuid4(),
        arguments={"deployment_id": "https://my-project.vercel.app/some/path"},
    )

    mock_get.assert_awaited_once()
    assert mock_get.await_args.args[0] == "https://api.vercel.com/v2/deployments/my-project.vercel.app/events"
    assert "Logs for deployment my-project.vercel.app" in result
    assert "build started" in result
    assert "build finished" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
@patch("httpx.AsyncClient.post")
async def test_vercel_set_env(mock_post, mock_get_token):
    mock_get_token.return_value = "fake-token"
    mock_post.return_value = MagicMock(status_code=201, json=dict)

    result = await _vercel_set_env(
        agent_id=uuid.uuid4(),
        arguments={"project_name": "my-project", "key": "DATABASE_URL", "value": "postgres://..."},
    )
    assert "set successfully" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
async def test_vercel_set_env_missing_token_preserves_message(mock_get_token):
    mock_get_token.return_value = None

    result = await _vercel_set_env(
        agent_id=uuid.uuid4(),
        arguments={
            "project_name": "my-project",
            "key": "DATABASE_URL",
            "value": "postgres://...",
        },
    )

    assert result == "❌ Vercel Access Token is not configured."


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
@patch("httpx.AsyncClient.post")
@patch("httpx.AsyncClient.get")
@patch("httpx.AsyncClient.patch")
async def test_vercel_set_env_conflict_updates(mock_patch, mock_get, mock_post, mock_get_token):
    mock_get_token.return_value = "fake-token"

    # Mock conflict 403 ENV_ALREADY_EXISTS
    mock_post.return_value = MagicMock(status_code=403, text='{"error":{"code":"ENV_ALREADY_EXISTS"}}')
    # Mock list envs to retrieve ID
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"envs": [{"id": "env_abc", "key": "DATABASE_URL"}]}
    )
    # Mock patch request
    mock_patch.return_value = MagicMock(status_code=200, json=dict)

    result = await _vercel_set_env(
        agent_id=uuid.uuid4(),
        arguments={"project_name": "my-project", "key": "DATABASE_URL", "value": "postgres://new-value"},
    )
    assert "updated successfully" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_vercel_token")
@patch("httpx.AsyncClient.get")
async def test_vercel_manage_domain_check(mock_get, mock_get_token):
    mock_get_token.return_value = "fake-token"

    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"available": True, "price": 10, "period": 1})

    result = await _vercel_manage_domain(agent_id=uuid.uuid4(), arguments={"action": "check", "domain": "example.com"})
    assert "example.com" in result
    assert "Available for purchase: Yes" in result
    assert "$10" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_tool_config")
@patch("app.services.agent_tools._check_neon_quota_limit")
@patch("httpx.AsyncClient.get")
@patch("httpx.AsyncClient.post")
async def test_neon_create_database_auto_resolve_org_id(mock_post, mock_get, mock_quota, mock_get_config):
    mock_get_config.return_value = {"neon_api_key": "fake-key"}
    mock_quota.return_value = (False, "")

    # Mock GET for organizations (returns single org)
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"organizations": [{"id": "org-resolved-123", "name": "Test Org"}]}
    )

    # Mock POST for project creation
    mock_post.return_value = MagicMock(
        status_code=201,
        json=lambda: {"project": {"id": "proj_123"}, "connection_uri": "postgresql://user:pass@host/neondb"},
    )

    result = await _neon_create_database(agent_id=uuid.uuid4(), arguments={"project_name": "my-neon-project"})
    assert "database created successfully" in result
    assert "postgresql://user:pass@host/neondb" in result
    assert "proj_123" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_tool_config")
@patch("app.services.agent_tools._check_neon_quota_limit")
@patch("httpx.AsyncClient.post")
async def test_neon_create_database_with_provided_org_id(mock_post, mock_quota, mock_get_config):
    mock_get_config.return_value = {"neon_api_key": "fake-key"}
    mock_quota.return_value = (False, "")

    mock_post.return_value = MagicMock(
        status_code=201,
        json=lambda: {"project": {"id": "proj_123"}, "connection_uri": "postgresql://user:pass@host/neondb"},
    )

    result = await _neon_create_database(
        agent_id=uuid.uuid4(), arguments={"project_name": "my-neon-project", "org_id": "my-manual-org"}
    )
    assert "database created successfully" in result


@pytest.mark.asyncio
@patch("app.services.agent_tools._get_tool_config")
@patch("app.services.agent_tools._check_neon_quota_limit")
async def test_neon_create_database_quota_blocked_passthrough(mock_quota, mock_get_config):
    quota_message = "quota blocked exactly"
    mock_get_config.return_value = {"neon_api_key": "fake-key"}
    mock_quota.return_value = (True, quota_message)

    result = await _neon_create_database(
        agent_id=uuid.uuid4(),
        arguments={"project_name": "my-neon-project"},
    )

    assert result == quota_message


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_get_vercel_quota_summary_returns_fallback_when_httpx_raises(mock_get):
    mock_get.side_effect = RuntimeError("network disabled")

    result = await _get_vercel_quota_summary("fake-token")

    assert result == "📊 **Vercel Account status**: Active (Quota details unavailable)"


@pytest.mark.asyncio
async def test_extracted_deploy_facade_wrappers_delegate_to_modules(monkeypatch, tmp_path):
    deploy = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy")
    deploy_ops = importlib.import_module("app.services.agent_tool_exec._agent_tool_exec_deploy_ops")
    agent_id = uuid.uuid4()

    async def extracted_deploy(observed_agent_id, observed_ws, arguments):
        return f"deploy:{observed_agent_id}:{observed_ws}:{arguments['project_name']}"

    async def extracted_env(observed_agent_id, arguments):
        return f"env:{observed_agent_id}:{arguments['project_name']}"

    async def extracted_neon(observed_agent_id, arguments):
        return f"neon:{observed_agent_id}:{arguments['project_name']}"

    monkeypatch.setattr(deploy, "_vercel_deploy", extracted_deploy)
    monkeypatch.setattr(deploy_ops, "_vercel_set_env", extracted_env)
    monkeypatch.setattr(deploy_ops, "_neon_create_database", extracted_neon)

    deployed = await _vercel_deploy(agent_id, tmp_path, {"project_name": "app"})
    env = await _vercel_set_env(agent_id, {"project_name": "app"})
    neon = await _neon_create_database(agent_id, {"project_name": "db"})

    assert deployed == f"deploy:{agent_id}:{tmp_path}:app"
    assert env == f"env:{agent_id}:app"
    assert neon == f"neon:{agent_id}:db"
