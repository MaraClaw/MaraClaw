"""Dynamic tool configuration loading with cache-aware dependency injection."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeIs

from app.config import get_settings
from app.core.json_types import JsonObject, json_object_from
from app.core.security import decrypt_data
from app.core.tool_types import ToolConfigSchema
from app.dao.agent_dao import agent_dao
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.services.tool_config import get_tenant_tool_config
from app.services.tool_runtime.provenance import ConfigLayer, merge_layers

SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret", "atlassian_api_key"}
_tool_config_cache: dict[tuple[str | None, str], tuple[JsonObject, datetime]] = {}
_TOOL_CONFIG_CACHE_TTL_SECONDS = 60


class ToolConfigDecryptionError(ValueError):
    """Stored tool credentials could not be decrypted; deliberately carries no detail."""

    def __init__(self) -> None:
        super().__init__("Tool configuration credentials could not be decrypted")


@dataclass(frozen=True, slots=True)
class ToolConfigDependencies:
    """Facade-provided collaborators captured for one configuration lookup."""

    decrypt_sensitive_fields: Callable[[JsonObject, ToolConfigSchema | None], JsonObject]
    get_cached_tool_config: Callable[[uuid.UUID | None, str], JsonObject | None]
    set_cached_tool_config: Callable[[uuid.UUID | None, str, JsonObject], None]


def _is_tool_config_schema(value: object) -> TypeIs[ToolConfigSchema]:
    if not isinstance(value, dict):
        return False
    fields: object | None = value.get("fields")
    if fields is None:
        return True
    if not isinstance(fields, list):
        return False
    return all(
        isinstance(field, dict)
        and (field.get("key") is None or isinstance(field.get("key"), str))
        and (field.get("type") is None or isinstance(field.get("type"), str))
        for field in list[object](fields)
    )


def _as_tool_config_schema(value: object) -> ToolConfigSchema | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        value = dict[object, object](value) if isinstance(value, Mapping) else {}
    return value if _is_tool_config_schema(value) else None


def _legacy_local_now() -> datetime:
    """Produce the legacy naive local timestamp from a timezone-aware clock."""
    return datetime.now(UTC).astimezone().replace(tzinfo=None)


def decrypt_sensitive_fields(
    config: JsonObject,
    config_schema: Mapping[str, Any] | ToolConfigSchema | None = None,
    *,
    sensitive_field_keys: set[str] | None = None,
) -> JsonObject:
    """Decrypt sensitive configuration fields without mutating the caller's mapping."""
    if not config:
        return config

    settings = get_settings()
    result = dict(config)
    sensitive_keys = set(SENSITIVE_FIELD_KEYS if sensitive_field_keys is None else sensitive_field_keys)
    if config_schema is not None and config_schema and "fields" in config_schema:
        for field in config_schema["fields"]:
            if field.get("type") == "password":
                key = field.get("key")
                if key is not None and key:
                    sensitive_keys.add(key)

    for key in sensitive_keys:
        if (value := result.get(key)) is not None and value:
            value = result[key]
            if isinstance(value, str) and value:
                result[key] = decrypt_data(value, settings.SECRET_KEY)
    return result


def get_cached_tool_config(
    agent_id: uuid.UUID | None,
    tool_name: str,
    *,
    cache: dict[tuple[str | None, str], tuple[JsonObject, datetime]] | None = None,
    now: datetime | None = None,
) -> JsonObject | None:
    """Return an unexpired cached configuration using the legacy UUID-normalized key."""
    cache_store = _tool_config_cache if cache is None else cache
    cache_key = (str(agent_id) if agent_id else None, tool_name)
    cached = cache_store.get(cache_key)
    if cached is None:
        return None
    config, expiry = cached
    if (_legacy_local_now() if now is None else now) < expiry:
        return config
    del cache_store[cache_key]
    return None


def set_cached_tool_config(
    agent_id: uuid.UUID | None,
    tool_name: str,
    config: JsonObject,
    *,
    cache: dict[tuple[str | None, str], tuple[JsonObject, datetime]] | None = None,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> None:
    """Store a configuration under the legacy cache key and TTL semantics."""
    cache_store = _tool_config_cache if cache is None else cache
    ttl = _TOOL_CONFIG_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    cache_key = (str(agent_id) if agent_id else None, tool_name)
    timestamp = _legacy_local_now() if now is None else now
    cache_store[cache_key] = (config, timestamp + timedelta(seconds=ttl))


def _decrypt_layer(
    dependencies: ToolConfigDependencies, config: JsonObject, schema: ToolConfigSchema | None
) -> JsonObject:
    try:
        return dependencies.decrypt_sensitive_fields(config, schema)
    except ValueError:
        raise ToolConfigDecryptionError from None


async def _tenant_layer(tenant_id: uuid.UUID | None, tool_name: str, schema: ToolConfigSchema | None) -> JsonObject:
    try:
        return await get_tenant_tool_config(tenant_id, tool_name, schema)
    except ValueError:
        raise ToolConfigDecryptionError from None


def _global_layer(source: object) -> ConfigLayer:
    return ConfigLayer.BUILTIN_GLOBAL if source == "builtin" else ConfigLayer.CUSTOM_GLOBAL


async def get_tool_config(
    agent_id: uuid.UUID | None,
    tool_name: str,
    *,
    dependencies: ToolConfigDependencies,
) -> JsonObject | None:
    """Load merged global, tenant, and agent config through facade-owned collaborators."""
    cached = dependencies.get_cached_tool_config(agent_id, tool_name)
    if cached is not None:
        return cached

    agent_tenant_id = None
    if agent_id:
        agent = await agent_dao.get(agent_id)
        agent_tenant_id = agent.tenant_id if agent else None

    if agent_id:
        joined = await agent_tool_dao.get_assignment_with_tool_by_name(agent_id, tool_name)
        if joined:
            assignment, tool_fields = joined
            agent_config = json_object_from(assignment.config)
            global_config = dict(json_object_from(tool_fields["config"]))
            tenant_config: JsonObject = {}
            schema = _as_tool_config_schema(tool_fields["config_schema"])
            if tool_fields["source"] == "builtin":
                tenant_config = await _tenant_layer(agent_tenant_id, tool_fields["name"], schema)
            merged = merge_layers(
                [
                    (_global_layer(tool_fields["source"]), _decrypt_layer(dependencies, global_config, schema)),
                    (ConfigLayer.TENANT, tenant_config),
                    (ConfigLayer.AGENT, _decrypt_layer(dependencies, agent_config, schema)),
                ]
            )
            if merged:
                dependencies.set_cached_tool_config(agent_id, tool_name, merged)
                return merged

    tool = await tool_dao.get_by_name(tool_name)
    if not tool:
        return None
    tenant_config = {}
    schema = _as_tool_config_schema(tool.config_schema)
    if tool.source == "builtin":
        tenant_config = await _tenant_layer(agent_tenant_id, tool.name, schema)
    merged = merge_layers(
        [
            (_global_layer(tool.source), _decrypt_layer(dependencies, json_object_from(tool.config), schema)),
            (ConfigLayer.TENANT, tenant_config),
        ]
    )
    if not merged:
        return None
    dependencies.set_cached_tool_config(agent_id, tool_name, merged)
    return merged
