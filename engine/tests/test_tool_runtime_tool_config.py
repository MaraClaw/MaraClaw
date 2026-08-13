import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.json_types import JsonObject
from app.services import agent_tools
from app.services.tool_runtime import tool_config


class _Result:
    def __init__(self, *, scalar_value=None, row=None):
        self._scalar_value = scalar_value
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar_value

    def first(self):
        return self._row


class _Session:
    def __init__(self, responses):
        self._responses = list(responses)

    async def execute(self, _statement):
        if not self._responses:
            raise AssertionError("unexpected execute call")
        return self._responses.pop(0)


class _SessionFactory:
    def __init__(self, *sessions):
        self._sessions = list(sessions)
        self._active_session = None

    def __call__(self):
        if not self._sessions:
            raise AssertionError("unexpected async_session call")
        self._active_session = self._sessions.pop(0)
        return self

    async def __aenter__(self):
        return self._active_session

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


def test_tool_config_cache_normalizes_uuid_and_expires_entries():
    # Given: an isolated runtime cache and a fixed clock.
    agent_id = uuid.uuid4()
    cache = {}
    config: JsonObject = {"api_key": "cached"}
    now = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)

    # When: a config is cached for an agent UUID.
    tool_config.set_cached_tool_config(
        agent_id,
        "search",
        config,
        cache=cache,
        ttl_seconds=60,
        now=now,
    )

    # Then: UUID lookup hits before expiry and removes the entry at expiry.
    assert (
        tool_config.get_cached_tool_config(agent_id, "search", cache=cache, now=now + timedelta(seconds=59)) is config
    )
    assert tool_config.get_cached_tool_config(agent_id, "search", cache=cache, now=now + timedelta(seconds=60)) is None
    assert cache == {}


async def test_tool_config_merges_global_tenant_and_agent_values_in_precedence_order(monkeypatch):
    # Given: one builtin assignment with distinct values at every configuration layer.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    assignment = SimpleNamespace(config={"shared": "agent", "agent_only": "agent"})
    tool_fields = {
        "config": {"shared": "global", "global_only": "global"},
        "config_schema": {"fields": [{"key": "secret_key", "type": "password"}]},
        "source": "builtin",
        "name": "configured_tool",
    }
    cached = []

    async def get_tenant_config(_db, received_tenant_id, tool_name, config_schema):
        assert received_tenant_id == tenant_id
        assert tool_name == "configured_tool"
        assert config_schema == {"fields": [{"key": "secret_key", "type": "password"}]}
        return {"shared": "tenant", "tenant_only": "tenant"}

    monkeypatch.setattr(tool_config, "get_tenant_tool_config", get_tenant_config)
    monkeypatch.setattr(tool_config.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(tenant_id=tenant_id)))
    monkeypatch.setattr(
        tool_config.agent_tool_dao,
        "get_assignment_with_tool_by_name",
        AsyncMock(return_value=(assignment, tool_fields)),
    )
    dependencies = tool_config.ToolConfigDependencies(
        session_factory=_SessionFactory(_Session(())),
        decrypt_sensitive_fields=lambda config, _schema: dict(config),
        get_cached_tool_config=lambda _agent_id, _tool_name: None,
        set_cached_tool_config=lambda _agent_id, _tool_name, config: cached.append(config),
    )

    # When: the runtime loads the config.
    result = await tool_config.get_tool_config(agent_id, "configured_tool", dependencies=dependencies)

    # Then: agent overrides tenant, tenant overrides global, and the merged value is cached.
    assert result == {
        "shared": "agent",
        "global_only": "global",
        "tenant_only": "tenant",
        "agent_only": "agent",
    }
    assert cached == [result]


async def test_tool_config_does_not_cache_an_empty_result(monkeypatch):
    # Given: no assignment or global tool row exists.
    from unittest.mock import AsyncMock

    cache_sets = []
    monkeypatch.setattr(tool_config.agent_dao, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_config.agent_tool_dao, "get_assignment_with_tool_by_name", AsyncMock(return_value=None))
    monkeypatch.setattr(tool_config.tool_dao, "get_by_name", AsyncMock(return_value=None))
    dependencies = tool_config.ToolConfigDependencies(
        session_factory=_SessionFactory(_Session(())),
        decrypt_sensitive_fields=lambda config, _schema: dict(config),
        get_cached_tool_config=lambda _agent_id, _tool_name: None,
        set_cached_tool_config=lambda _agent_id, _tool_name, config: cache_sets.append(config),
    )

    # When: the runtime cannot find configuration for the requested tool.
    result = await tool_config.get_tool_config(uuid.uuid4(), "missing", dependencies=dependencies)

    # Then: it returns None without caching an empty configuration.
    assert result is None
    assert cache_sets == []


def test_decrypt_sensitive_fields_handles_schema_atlassian_plaintext_and_input_immutability(monkeypatch):
    # Given: encrypted hardcoded and schema password fields plus a plaintext fallback.
    source: JsonObject = {
        "atlassian_api_key": "encrypted-atlassian",
        "schema_key": "encrypted-schema",
        "api_key": "plain",
    }
    monkeypatch.setattr(tool_config, "get_settings", lambda: SimpleNamespace(SECRET_KEY="secret"))

    def decrypt(value, _secret_key):
        if value == "plain":
            raise ValueError("already plaintext")
        return f"decrypted:{value}"

    monkeypatch.setattr(tool_config, "decrypt_data", decrypt)

    # When: runtime decryption receives the tool schema.
    result = tool_config.decrypt_sensitive_fields(
        source,
        {"fields": [{"key": "schema_key", "type": "password"}]},
    )

    # Then: all sensitive values are handled while the caller's config remains unchanged.
    assert result == {
        "atlassian_api_key": "decrypted:encrypted-atlassian",
        "schema_key": "decrypted:encrypted-schema",
        "api_key": "plain",
    }
    assert source == {"atlassian_api_key": "encrypted-atlassian", "schema_key": "encrypted-schema", "api_key": "plain"}


async def test_facade_builds_fresh_tool_config_dependencies_from_current_globals(monkeypatch):
    # Given: the runtime implementation records each facade-created dependency bundle.
    observed = []

    async def get_tool_config(_agent_id, _tool_name, *, dependencies):
        observed.append(dependencies)

    monkeypatch.setattr(tool_config, "get_tool_config", get_tool_config)

    # When: the facade builds dependencies on successive invocations (DAO path; no session factory).
    await agent_tools._get_tool_config(uuid.uuid4(), "tool")
    await agent_tools._get_tool_config(uuid.uuid4(), "tool")

    # Then: each invocation receives a distinct bundle sourced from current facade globals.
    assert observed[0] is not observed[1]
    assert observed[0].decrypt_sensitive_fields is agent_tools._decrypt_sensitive_fields
    assert observed[1].get_cached_tool_config is agent_tools._get_cached_tool_config
    assert observed[0].set_cached_tool_config is agent_tools._set_cached_tool_config
