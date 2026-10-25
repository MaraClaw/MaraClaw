from collections.abc import Iterator
from datetime import datetime
from functools import partial
from types import MappingProxyType, SimpleNamespace
from typing import Self, assert_type
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.core.json_types import JsonObject
from app.core.security import encrypt_data
from app.dao.tool_assignment_metadata import ToolAssignmentMetadata
from app.dao.tool_dao import AgentToolDAO
from app.db import session as session_module
from app.db.connection import Row
from app.records.tool import AgentToolRecord
from app.services.tool_runtime import tool_config


class _Cursor:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw: _RawConnection = raw

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass

    async def execute(self, query: object, params: object = None) -> None:
        self.raw.executed.append((str(query), params))

    async def fetchone(self) -> Row | None:
        return self.raw.row


class _RawConnection:
    def __init__(self) -> None:
        self.row: Row | None = None
        self.executed: list[tuple[str, object]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _PoolConnection:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw: _RawConnection = raw

    async def __aenter__(self) -> _RawConnection:
        return self.raw

    async def __aexit__(self, *_args: object) -> None:
        pass


class _Pool:
    def __init__(self, raw: _RawConnection) -> None:
        self.raw: _RawConnection = raw

    def connection(self) -> _PoolConnection:
        return _PoolConnection(self.raw)


@pytest.fixture
def raw_connection(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RawConnection]:
    raw = _RawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _Pool(raw))
    token = session_module._conn_ctx.set(None)
    try:
        yield raw
    finally:
        session_module._conn_ctx.reset(token)


def _assignment_row(agent_id: UUID) -> Row:
    return {"id": str(uuid4()), "agent_id": str(agent_id), "tool_id": str(uuid4()), "enabled": 1}


async def test_join_produces_fixed_metadata_without_restricting_payloads(raw_connection: _RawConnection) -> None:
    agent_id = uuid4()
    config: JsonObject = {"vendor_options": {"regions": ["east", None], "enabled": True}}
    schema = MappingProxyType({"fields": [], "vendor_extension": object()})
    row = _assignment_row(agent_id)
    row.update(tool_config=config, tool_config_schema=schema, tool_source="custom", tool_name="catalog_name")
    raw_connection.row = row

    joined = await AgentToolDAO().get_assignment_with_tool_by_name(agent_id, "requested_name")

    assert joined is not None
    assignment, metadata = joined
    assert_type(metadata, ToolAssignmentMetadata)
    assert_type(metadata["source"], str)
    assert_type(metadata["name"], str)
    assert assignment == AgentToolRecord(UUID(str(row["id"])), agent_id, UUID(str(row["tool_id"])))
    assert set(metadata) == {"config", "config_schema", "source", "name"}
    assert metadata["config"] is config
    assert metadata["config_schema"] is schema
    assert metadata["source"] == "custom"
    assert metadata["name"] == "catalog_name"
    query, params = raw_connection.executed[0]
    assert "JOIN tools t ON t.id = at.tool_id" in query
    assert "t.config AS tool_config, t.config_schema AS tool_config_schema" in query
    assert "at.agent_id = %(agent_id)s AND t.name = %(tool_name)s" in query
    assert params == {"agent_id": agent_id, "tool_name": "requested_name"}


@pytest.mark.parametrize("empty_value", [None, "", False, 0, [], {}])
async def test_join_retains_falsy_row_defaults(raw_connection: _RawConnection, empty_value: object) -> None:
    agent_id = uuid4()
    row = _assignment_row(agent_id)
    row.update(dict.fromkeys(("config", "tool_config", "tool_config_schema", "tool_source", "tool_name"), empty_value))
    raw_connection.row = row

    joined = await AgentToolDAO().get_assignment_with_tool_by_name(agent_id, "requested_name")

    assert joined is not None
    assignment, metadata = joined
    assert assignment.config == {}
    assert metadata == {"config": {}, "config_schema": {}, "source": "builtin", "name": "requested_name"}


async def test_join_returns_none_when_no_assignment_exists(raw_connection: _RawConnection) -> None:
    joined = await AgentToolDAO().get_assignment_with_tool_by_name(uuid4(), "missing")

    assert joined is None


@pytest.mark.parametrize("source", ["builtin", "custom"])
async def test_runtime_consumes_join_with_decryption_precedence_and_cache(
    raw_connection: _RawConnection, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    agent_id, tenant_id = uuid4(), uuid4()
    secret_key = tool_config.get_settings().SECRET_KEY
    global_config: JsonObject = {
        "shared": "global",
        "tenant_wins": "global",
        "api_key": encrypt_data("global-api", secret_key),
        "credential": encrypt_data("global-credential", secret_key),
        "vendor_options": {"retries": [1, None], "enabled": False},
    }
    agent_config: JsonObject = {"shared": "agent", "credential": encrypt_data("agent-credential", secret_key)}
    schema = MappingProxyType(
        {
            "fields": [{"key": "credential", "type": "password", "vendor_extension": object()}],
            "vendor_extension": object(),
        }
    )
    row = _assignment_row(agent_id)
    row.update(
        config=agent_config,
        tool_config=global_config,
        tool_config_schema=schema,
        tool_source=source,
        tool_name="catalog_name",
    )
    raw_connection.row = row
    tenant_lookup = AsyncMock(return_value={"shared": "tenant", "tenant_wins": "tenant"})
    monkeypatch.setattr(tool_config, "get_tenant_tool_config", tenant_lookup)
    monkeypatch.setattr(tool_config.agent_dao, "get", AsyncMock(return_value=SimpleNamespace(tenant_id=tenant_id)))
    cache: dict[tuple[str | None, str], tuple[JsonObject, datetime]] = {}
    dependencies = tool_config.ToolConfigDependencies(
        decrypt_sensitive_fields=tool_config.decrypt_sensitive_fields,
        get_cached_tool_config=partial(tool_config.get_cached_tool_config, cache=cache),
        set_cached_tool_config=partial(tool_config.set_cached_tool_config, cache=cache),
    )

    result = await tool_config.get_tool_config(agent_id, "requested_name", dependencies=dependencies)

    assert result == {
        "shared": "agent",
        "tenant_wins": "tenant" if source == "builtin" else "global",
        "api_key": "global-api",
        "credential": "agent-credential",
        "vendor_options": {"retries": [1, None], "enabled": False},
    }
    assert cache[(str(agent_id), "requested_name")][0] is result
    assert global_config["api_key"] != "global-api"
    assert agent_config["credential"] != "agent-credential"
    if source == "builtin":
        tenant_lookup.assert_awaited_once_with(tenant_id, "catalog_name", dict(schema))
    else:
        tenant_lookup.assert_not_awaited()


@pytest.mark.parametrize("fields", ["invalid", [1], [{"key": 1}], [{"type": False}]])
def test_schema_guard_rejects_only_malformed_decryption_fields(fields: object) -> None:
    schema = MappingProxyType({"fields": fields, "vendor_extension": object()})

    result = tool_config._as_tool_config_schema(schema)

    assert result is None
