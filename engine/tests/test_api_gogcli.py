import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.api import gogcli as gogcli_api
from app.api.gogcli import GogcliKeyringSecretRequest, set_gogcli_keyring_secret
from app.services import gogcli_runtime


def _user(role: str = "member"):
    return SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=uuid.uuid4(), display_name="Test User")


def _db() -> object:
    return SimpleNamespace(close=lambda: None)


def _agent(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), gogcli_enabled=enabled)


def _configure_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(gogcli_runtime.settings, "STORAGE_LOCAL_ROOT", str(tmp_path / "local-root"))
    monkeypatch.setattr(gogcli_runtime.settings, "AGENT_DATA_DIR", str(tmp_path / "agent-data"))


async def test_keyring_secret_endpoint_writes_secret_for_manager(monkeypatch, tmp_path) -> None:
    # Given
    _configure_runtime(monkeypatch, tmp_path)
    agent = _agent(enabled=True)

    async def check_access(_current_user, agent_id, _db=None):
        assert agent_id == agent.id
        return agent, "manage"

    persist_calls = []

    async def persist_secret(_db, agent_id, password):
        persist_calls.append((agent_id, password))

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "upsert_gogcli_keyring_password", persist_secret)

    # When
    result = await set_gogcli_keyring_secret(
        agent.id,
        GogcliKeyringSecretRequest(password=SecretStr("correct horse battery staple")),
        _user(),
        _db(),
    )

    # Then
    secret_file = gogcli_runtime.gogcli_secret_file(agent.id)
    assert result is None
    assert secret_file.read_text(encoding="utf-8") == "correct horse battery staple"
    assert persist_calls == [(agent.id, "correct horse battery staple")]


async def test_keyring_secret_endpoint_offloads_blocking_write(monkeypatch, tmp_path) -> None:
    # Given
    _configure_runtime(monkeypatch, tmp_path)
    agent = _agent(enabled=True)
    offload_calls = []

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    def blocking_write(_agent_id, _password):
        msg = "write_gogcli_keyring_secret must be offloaded through anyio.to_thread.run_sync"
        raise AssertionError(msg)

    async def run_sync(function, *args):
        offload_calls.append((function, args))
        return tmp_path / "stored-secret"

    async def persist_secret(_db, _agent_id, _password):
        return None

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "write_gogcli_keyring_secret", blocking_write)
    monkeypatch.setattr(gogcli_api, "run_sync", run_sync)
    monkeypatch.setattr(gogcli_api, "upsert_gogcli_keyring_password", persist_secret)

    # When
    result = await set_gogcli_keyring_secret(
        agent.id,
        GogcliKeyringSecretRequest(password=SecretStr("correct horse battery staple")),
        _user(),
        _db(),
    )

    # Then
    assert result is None
    assert offload_calls == [(blocking_write, (agent.id, "correct horse battery staple"))]


async def test_keyring_secret_endpoint_requires_manage_access(monkeypatch, tmp_path) -> None:
    # Given
    _configure_runtime(monkeypatch, tmp_path)
    agent = _agent(enabled=True)

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "use"

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await set_gogcli_keyring_secret(
            agent.id,
            GogcliKeyringSecretRequest(password=SecretStr("secret")),
            _user(),
            _db(),
        )
    assert exc_info.value.status_code == 403


async def test_keyring_secret_endpoint_rejects_non_gogcli_agent(monkeypatch, tmp_path) -> None:
    # Given
    _configure_runtime(monkeypatch, tmp_path)
    agent = _agent(enabled=False)

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await set_gogcli_keyring_secret(
            agent.id,
            GogcliKeyringSecretRequest(password=SecretStr("secret")),
            _user(),
            _db(),
        )
    assert exc_info.value.status_code == 400


async def test_start_auth_endpoint_requires_manage_access(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "use"

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    start_auth = gogcli_api.start_gogcli_auth
    start_request = gogcli_api.GogcliAuthStartRequest

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await start_auth(agent.id, start_request(account_email="maraclaw@example.com"), _user(), _db())
    assert exc_info.value.status_code == 403


async def test_start_auth_endpoint_rejects_non_gogcli_agent(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=False)

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    start_auth = gogcli_api.start_gogcli_auth
    start_request = gogcli_api.GogcliAuthStartRequest

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await start_auth(agent.id, start_request(account_email="maraclaw@example.com"), _user(), _db())
    assert exc_info.value.status_code == 400


async def test_start_auth_endpoint_reports_missing_container_without_raw_output(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)
    agent.container_id = None
    raw_output = "raw-gog-stdout-should-not-leak"

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def failing_start(_docker_client, _agent, _account_email):
        return SimpleNamespace(started=False, auth_url=None, detail=raw_output, raw_stdout=raw_output)

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "start_gogcli_auth_handoff", failing_start)
    start_auth = gogcli_api.start_gogcli_auth
    start_request = gogcli_api.GogcliAuthStartRequest

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await start_auth(agent.id, start_request(account_email="maraclaw@example.com"), _user(), _db())
    assert exc_info.value.status_code == 409
    assert raw_output not in str(exc_info.value.detail)


async def test_start_auth_endpoint_returns_sanitized_auth_url(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)
    agent.container_id = "container-oauth"
    raw_output = "raw-gog-stdout-should-not-leak"

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def starting_auth(_docker_client, _agent, _account_email):
        return SimpleNamespace(
            started=True,
            auth_url="https://accounts.google.com/o/oauth2/v2/auth?client_id=public-client",
            detail="Open the authorization URL in a browser",
            raw_stdout=raw_output,
            raw_stderr=raw_output,
        )

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "start_gogcli_auth_handoff", starting_auth)
    start_auth = gogcli_api.start_gogcli_auth
    start_request = gogcli_api.GogcliAuthStartRequest

    # When
    response = await start_auth(agent.id, start_request(account_email="maraclaw@example.com"), _user(), _db())

    # Then
    payload = response.model_dump()
    assert payload == {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=public-client",
        "detail": "Open the authorization URL in a browser",
    }
    assert raw_output not in repr(payload)


async def test_auth_status_endpoint_requires_manage_access(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "use"

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    get_status = gogcli_api.get_gogcli_auth_status

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await get_status(agent.id, _user(), _db())
    assert exc_info.value.status_code == 403


async def test_auth_status_endpoint_rejects_non_gogcli_agent(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=False)

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    get_status = gogcli_api.get_gogcli_auth_status

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await get_status(agent.id, _user(), _db())
    assert exc_info.value.status_code == 400


async def test_auth_status_endpoint_reports_missing_container_without_raw_output(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)
    agent.container_id = None
    raw_output = "raw-gog-stderr-should-not-leak"

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def missing_container_status(_docker_client, _agent):
        return SimpleNamespace(authenticated=False, account_hint=None, detail=raw_output, raw_stderr=raw_output)

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "read_gogcli_auth_status", missing_container_status)
    get_status = gogcli_api.get_gogcli_auth_status

    # When / Then
    with pytest.raises(HTTPException) as exc_info:
        await get_status(agent.id, _user(), _db())
    assert exc_info.value.status_code == 409
    assert raw_output not in str(exc_info.value.detail)


async def test_auth_status_endpoint_returns_sanitized_status(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)
    agent.container_id = "container-oauth"
    raw_output = "raw-gog-status-output-should-not-leak"

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def authenticated_status(_docker_client, _agent):
        return SimpleNamespace(
            authenticated=True,
            account_hint="maraclaw@example.com",
            detail="Authenticated",
            raw_stdout=raw_output,
            raw_stderr=raw_output,
        )

    capture_calls = []

    async def capture_state(_db, agent_id, _agent_dir, status):
        capture_calls.append((agent_id, status.authenticated, status.account_hint))

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "read_gogcli_auth_status", authenticated_status)
    monkeypatch.setattr(gogcli_api, "capture_authenticated_gogcli_state", capture_state)
    get_status = gogcli_api.get_gogcli_auth_status

    # When
    response = await get_status(agent.id, _user(), _db())

    # Then
    payload = response.model_dump()
    assert payload == {
        "authenticated": True,
        "account_hint": "maraclaw@example.com",
        "detail": "Authenticated",
    }
    assert raw_output not in repr(payload)
    assert capture_calls == [(agent.id, True, "maraclaw@example.com")]


async def test_auth_status_endpoint_marks_snapshotted_state_needs_reauth(monkeypatch) -> None:
    # Given
    agent = _agent(enabled=True)
    agent.container_id = "container-oauth"

    async def check_access(_current_user, _agent_id, _db=None):
        return agent, "manage"

    async def unauthenticated_status(_docker_client, _agent):
        return SimpleNamespace(authenticated=False, account_hint=None, detail="Not authenticated")

    async def mark_needs_reauth(_db, agent_id):
        assert agent_id == agent.id
        return True

    monkeypatch.setattr(gogcli_api, "check_agent_access", check_access)
    monkeypatch.setattr(gogcli_api, "read_gogcli_auth_status", unauthenticated_status)
    monkeypatch.setattr(gogcli_api, "mark_gogcli_needs_reauth_if_snapshot_exists", mark_needs_reauth)

    # When
    response = await gogcli_api.get_gogcli_auth_status(agent.id, _user(), _db())

    # Then
    assert response.model_dump() == {
        "authenticated": False,
        "account_hint": None,
        "detail": "Needs re-authentication",
    }
