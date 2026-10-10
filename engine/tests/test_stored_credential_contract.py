import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.security import encrypt_data
from app.dao.agent_dao import AgentDAO
from app.services import resource_discovery, tool_config
from app.services.agent_tool_exec import email_tools
from app.services.llm import utils
from app.services.openclaw_keys import mint_openclaw_gateway_key
from app.services.tool_runtime import tool_config as runtime_config


@dataclass(frozen=True)
class StoredModel:
    api_key_encrypted: str | None


@pytest.fixture
def secret_settings(monkeypatch):
    settings = SimpleNamespace(SECRET_KEY="test-credential-secret")
    for module in (utils, tool_config, runtime_config):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize("stored", ["plaintext-key", "not!ciphertext", "YWJj"])
def test_model_rejects_invalid_stored_credentials(secret_settings, stored):
    with pytest.raises(ValueError):
        utils.get_model_api_key(StoredModel(stored))


def test_model_decrypts_current_ciphertext(secret_settings):
    stored = encrypt_data("model-key", secret_settings.SECRET_KEY)
    assert utils.get_model_api_key(StoredModel(stored)) == "model-key"


@pytest.mark.parametrize("stored", [None, ""])
def test_absent_model_key_remains_optional(stored):
    assert utils.get_model_api_key(StoredModel(stored)) == ""


@pytest.mark.parametrize(
    "reader",
    [
        tool_config.decrypt_sensitive_fields,
        runtime_config.decrypt_sensitive_fields,
        email_tools._decrypt_sensitive_fields,
    ],
)
@pytest.mark.parametrize("stored", ["plaintext-key", "YWJj"])
def test_sensitive_config_rejects_invalid_ciphertext(secret_settings, reader, stored):
    with pytest.raises(ValueError):
        reader({"api_key": stored})


@pytest.mark.parametrize(
    "reader",
    [
        tool_config.decrypt_sensitive_fields,
        runtime_config.decrypt_sensitive_fields,
        email_tools._decrypt_sensitive_fields,
    ],
)
def test_sensitive_config_current_writer_round_trip(secret_settings, reader):
    schema = {"fields": [{"key": "custom_key", "type": "password"}]}
    source = {"api_key": "key", "custom_key": "custom", "ordinary": "unchanged"}
    stored = tool_config.encrypt_sensitive_fields(source, schema)
    assert reader(stored, schema) == source
    assert stored["api_key"] != source["api_key"]


@pytest.mark.parametrize("case", ["canonical", "raw-row", "hash-as-token"])
async def test_gateway_accepts_only_token_against_canonical_hash(monkeypatch, case):
    token, digest = mint_openclaw_gateway_key()
    presented = digest if case == "hash-as-token" else token
    stored = token if case == "raw-row" else digest
    agent_id = uuid.uuid4()

    async def fetchone(_sql, params):
        return (
            {"id": agent_id, "name": "gateway", "creator_id": uuid.uuid4(), "api_key_hash": stored}
            if params["key"] == stored
            else None
        )

    @asynccontextmanager
    async def session(_self):
        yield SimpleNamespace(fetchone=fetchone)

    monkeypatch.setattr(AgentDAO, "session", session)
    result = await AgentDAO().get_openclaw_by_api_key(presented)
    assert (result.id if result else None) == (agent_id if case == "canonical" else None)


@pytest.mark.parametrize(
    ("stored", "expected"), [("plaintext-key", ""), ("YWJj", ""), (None, ""), ("encrypted", "key")]
)
async def test_smithery_optional_key_rejects_old_storage(secret_settings, monkeypatch, stored, expected):
    value = encrypt_data("key", secret_settings.SECRET_KEY) if stored == "encrypted" else stored
    monkeypatch.setattr(
        resource_discovery.tool_dao,
        "get_by_name",
        AsyncMock(
            return_value=SimpleNamespace(
                name="discover_resources", config={"smithery_api_key": value}, config_schema={}
            )
        ),
    )
    assert await resource_discovery._get_smithery_api_key() == expected


async def test_runtime_merges_decrypted_tenant_with_encrypted_agent_config(secret_settings, monkeypatch):
    tenant_id = uuid.uuid4()
    schema = {"fields": [{"key": "tenant_secret", "type": "password"}]}
    assignment = SimpleNamespace(config=tool_config.encrypt_sensitive_fields({"api_key": "agent-key"}))
    fields = {"config": {}, "config_schema": schema, "source": "builtin", "name": "configured"}
    monkeypatch.setattr(runtime_config.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(tenant_id=tenant_id)))
    monkeypatch.setattr(
        runtime_config.agent_tool_dao, "get_assignment_with_tool_by_name", AsyncMock(return_value=(assignment, fields))
    )
    monkeypatch.setattr(
        runtime_config, "get_tenant_tool_config", AsyncMock(return_value={"tenant_secret": "tenant-key"})
    )
    dependencies = runtime_config.ToolConfigDependencies(
        decrypt_sensitive_fields=tool_config.decrypt_sensitive_fields,
        get_cached_tool_config=lambda _agent_id, _name: None,
        set_cached_tool_config=lambda _agent_id, _name, _config: None,
    )
    assert await runtime_config.get_tool_config(uuid.uuid4(), "configured", dependencies=dependencies) == {
        "tenant_secret": "tenant-key",
        "api_key": "agent-key",
    }


async def test_smithery_tenant_does_not_use_global_secret(secret_settings, monkeypatch):
    tenant_id = uuid.uuid4()
    monkeypatch.setattr(
        resource_discovery.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(tenant_id=tenant_id))
    )
    monkeypatch.setattr(resource_discovery.agent_tool_dao, "list_for_agent", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        resource_discovery.tool_dao,
        "get_by_name",
        AsyncMock(
            return_value=SimpleNamespace(
                name="discover_resources",
                config={"smithery_api_key": encrypt_data("global-key", secret_settings.SECRET_KEY)},
                config_schema={},
            )
        ),
    )
    monkeypatch.setattr(resource_discovery, "get_tenant_tool_config", AsyncMock(return_value={}))
    assert await resource_discovery._get_smithery_api_key(uuid.uuid4()) == ""


async def test_smithery_import_persists_current_ciphertext(secret_settings, monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(
        resource_discovery.tool_dao, "get_by_name", AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    )
    saved = AsyncMock()
    monkeypatch.setattr(resource_discovery.agent_tool_dao, "ensure_with_config", saved)
    monkeypatch.setattr(resource_discovery.tool_dao, "list_mcp_by_name_prefixes", AsyncMock(return_value=[]))

    def unavailable_http(**_kwargs):
        raise ValueError("external HTTP unavailable")

    monkeypatch.setattr(resource_discovery.httpx, "AsyncClient", unavailable_http)
    await resource_discovery.import_mcp_from_smithery("example", agent_id, {"smithery_api_key": "wire-key"})
    assert saved.await_count == 2
    for call in saved.await_args_list:
        assert call.kwargs["config"]["smithery_api_key"] != "wire-key"
        assert tool_config.decrypt_sensitive_fields(
            call.kwargs["config"], {"fields": [{"key": "smithery_api_key", "type": "password"}]}
        ) == {"smithery_api_key": "wire-key"}
