"""Dynamic tool configuration loading with cache-aware dependency injection."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.core.json_types import JsonObject
from app.core.security import decrypt_data
from app.core.tool_types import ToolConfigSchema
from app.dao.agent_dao import agent_dao
from app.dao.tool_dao import agent_tool_dao, tool_dao
from app.services.tool_config import get_tenant_tool_config

SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret", "atlassian_api_key"}
_tool_config_cache: dict[tuple[str | None, str], tuple[JsonObject, datetime]] = {}
_TOOL_CONFIG_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ToolConfigDependencies:
    """Facade-provided collaborators captured for one configuration lookup."""

    decrypt_sensitive_fields: Callable[[JsonObject, ToolConfigSchema | None], JsonObject]
    get_cached_tool_config: Callable[[uuid.UUID | None, str], JsonObject | None]
    set_cached_tool_config: Callable[[uuid.UUID | None, str, JsonObject], None]


def _legacy_local_now() -> datetime:
    """Produce the legacy naive local timestamp from a timezone-aware clock."""
    return datetime.now(UTC).astimezone().replace(tzinfo=None)


def decrypt_sensitive_fields(
    config: JsonObject,
    config_schema: ToolConfigSchema | None = None,
    *,
    sensitive_field_keys: set[str] | None = None,
) -> JsonObject:
    """Decrypt sensitive configuration fields without mutating the caller's mapping."""
    if not config:
        return config

    settings = get_settings()
    result = dict(config)
    sensitive_keys = set(SENSITIVE_FIELD_KEYS if sensitive_field_keys is None else sensitive_field_keys)
    if config_schema and "fields" in config_schema:
        for field in config_schema["fields"]:
            if field.get("type") == "password":
                key = field.get("key")
                if key:
                    sensitive_keys.add(key)

    for key in sensitive_keys:
        if result.get(key):
            value = result[key]
            if isinstance(value, str) and value:
                # Existing encrypted-config behavior treats an undecryptable value as plaintext.
                with suppress(Exception):
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
            agent_config = assignment.config or {}
            global_config = tool_fields.get("config") or {}
            config_schema = tool_fields.get("config_schema") or {}
            tool_source = tool_fields.get("source") or "builtin"
            db_tool_name = tool_fields.get("name") or tool_name
            tenant_config: JsonObject = {}
            if tool_source == "builtin":
                tenant_config = await get_tenant_tool_config(None, agent_tenant_id, db_tool_name, config_schema)
            if not isinstance(global_config, dict):
                global_config = dict(global_config)
            if not isinstance(config_schema, dict):
                config_schema = dict(config_schema)
            merged = {**global_config, **tenant_config, **agent_config}
            if merged:
                decrypted = dependencies.decrypt_sensitive_fields(merged, config_schema)  # type: ignore[arg-type]
                dependencies.set_cached_tool_config(agent_id, tool_name, decrypted)
                return decrypted

    tool = await tool_dao.get_by_name(tool_name)
    if not tool:
        return None
    tenant_config: JsonObject = {}
    if tool.source == "builtin":
        tenant_config = await get_tenant_tool_config(None, agent_tenant_id, tool.name, tool.config_schema)
    merged = {**(tool.config or {}), **tenant_config}
    if not merged:
        return None
    decrypted = dependencies.decrypt_sensitive_fields(merged, tool.config_schema)  # type: ignore[arg-type]
    dependencies.set_cached_tool_config(agent_id, tool_name, decrypted)
    return decrypted
