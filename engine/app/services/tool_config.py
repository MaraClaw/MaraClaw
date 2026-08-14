"""Tool configuration helpers.

Builtin tools are global capability records, so tenant/company configuration
must not live in ``tools.config`` for those rows. Tenant-specific values are
stored in ``tenant_settings`` under ``tool_config:<tool_name>``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.config import get_settings
from app.core.json_types import JsonObject, JsonValue
from app.core.logging import logger
from app.core.security import decrypt_data, encrypt_data
from app.core.tool_types import ToolConfigSchema
from app.db.session import connection_ctx
from app.db.types import as_jsonb
from app.records.tool import ToolRecord

SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret"}
TENANT_TOOL_CONFIG_PREFIX = "tool_config:"


def tenant_tool_config_key(tool_name: str) -> str:
    return f"{TENANT_TOOL_CONFIG_PREFIX}{tool_name}"


def get_sensitive_keys(config_schema: Mapping[str, Any] | ToolConfigSchema | None = None) -> set[str]:
    keys = set(SENSITIVE_FIELD_KEYS)
    if config_schema:
        for field in config_schema.get("fields", []):
            if field.get("type") == "password":
                keys.add(field.get("key", ""))
    keys.discard("")
    return keys


def encrypt_sensitive_fields(
    config: Mapping[str, JsonValue], config_schema: Mapping[str, Any] | ToolConfigSchema | None = None
) -> JsonObject:
    if not config:
        return {}

    settings = get_settings()
    result: JsonObject = dict(config)
    for key in get_sensitive_keys(config_schema):
        value = result.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            _ = decrypt_data(value, settings.SECRET_KEY)
            continue
        except ValueError as exc:
            logger.debug("Encrypting non-ciphertext tool configuration field {}: {}", key, type(exc).__name__)
            result[key] = encrypt_data(value, settings.SECRET_KEY)
    return result


def decrypt_sensitive_fields(
    config: Mapping[str, JsonValue], config_schema: Mapping[str, Any] | ToolConfigSchema | None = None
) -> JsonObject:
    if not config:
        return {}

    settings = get_settings()
    result: JsonObject = dict(config)
    for key in get_sensitive_keys(config_schema):
        value = result.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            result[key] = decrypt_data(value, settings.SECRET_KEY)
        except ValueError as exc:
            logger.debug("Leaving unreadable tool configuration field {} unchanged: {}", key, type(exc).__name__)
            continue
    return result


def meaningful_config(config: Mapping[str, JsonValue] | None) -> JsonObject:
    """Drop empty form values while preserving booleans/numbers."""
    if not config:
        return {}
    cleaned: JsonObject = {}
    for key, value in config.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value
    return cleaned


async def get_tenant_tool_config(
    db: object | None,
    tenant_id: uuid.UUID | None,
    tool_name: str,
    config_schema: Mapping[str, Any] | ToolConfigSchema | None = None,
) -> JsonObject:
    del db
    if not tenant_id:
        return {}
    async with connection_ctx() as conn:
        row = await conn.fetchone(
            "SELECT value FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
            {"tenant_id": tenant_id, "key": tenant_tool_config_key(tool_name)},
        )
    value_raw = row.get("value") if row else None
    value_map: dict[str, Any] = dict[str, Any](value_raw) if isinstance(value_raw, dict) else {}
    raw = value_map.get("config")
    if not isinstance(raw, dict):
        return {}
    return decrypt_sensitive_fields(raw, config_schema)


async def get_tenant_tool_configs(
    tenant_id: uuid.UUID | None,
    tools: Sequence[ToolRecord],
) -> dict[str, JsonObject]:
    """Load company configs for many tools in one tenant_settings round-trip."""
    if not tenant_id or not tools:
        return {}
    names = [getattr(tool, "name", None) for tool in tools]
    keys = [tenant_tool_config_key(name) for name in names if isinstance(name, str) and name]
    if not keys:
        return {}
    async with connection_ctx() as conn:
        rows = await conn.fetchall(
            "SELECT key, value FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = ANY(%(keys)s)",
            {"tenant_id": tenant_id, "keys": keys},
        )
    by_key = {row["key"]: row.get("value") for row in rows}
    result: dict[str, JsonObject] = {}
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            continue
        setting_raw = by_key.get(tenant_tool_config_key(name)) or {}
        setting_map: dict[str, Any] = dict[str, Any](setting_raw) if isinstance(setting_raw, dict) else {}
        raw = setting_map.get("config")
        if not isinstance(raw, dict):
            result[name] = {}
            continue
        result[name] = decrypt_sensitive_fields(raw, getattr(tool, "config_schema", None))
    return result


async def set_tenant_tool_config(
    db: object | None,
    tenant_id: uuid.UUID,
    tool_name: str,
    config: Mapping[str, JsonValue],
    config_schema: Mapping[str, Any] | ToolConfigSchema | None = None,
) -> None:
    del db
    encrypted = encrypt_sensitive_fields(meaningful_config(config), config_schema)
    stored_value: JsonObject = {"config": encrypted}
    key = tenant_tool_config_key(tool_name)
    async with connection_ctx() as conn:
        existing = await conn.fetchone(
            "SELECT tenant_id FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
            {"tenant_id": tenant_id, "key": key},
        )
        if existing:
            await conn.execute(
                "UPDATE tenant_settings SET value = %(value)s, updated_at = NOW() "
                + "WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
                {"tenant_id": tenant_id, "key": key, "value": as_jsonb(stored_value)},
            )
        else:
            await conn.execute(
                "INSERT INTO tenant_settings (tenant_id, key, value) VALUES (%(tenant_id)s, %(key)s, %(value)s)",
                {"tenant_id": tenant_id, "key": key, "value": as_jsonb(stored_value)},
            )


async def delete_tenant_tool_config(db: object | None, tenant_id: uuid.UUID, tool_name: str) -> None:
    del db
    async with connection_ctx() as conn:
        await conn.execute(
            "DELETE FROM tenant_settings WHERE tenant_id = %(tenant_id)s AND key = %(key)s",
            {"tenant_id": tenant_id, "key": tenant_tool_config_key(tool_name)},
        )


async def get_tool_company_config(db: object | None, tool: ToolRecord | Any, tenant_id: uuid.UUID | None) -> JsonObject:
    """Return company config for a tool without leaking builtin config across tenants."""
    if getattr(tool, "source", None) == "builtin":
        return await get_tenant_tool_config(db, tenant_id, tool.name, getattr(tool, "config_schema", None))
    return decrypt_sensitive_fields(getattr(tool, "config", None) or {}, getattr(tool, "config_schema", None))


def mask_sensitive_fields(
    config: Mapping[str, JsonValue], config_schema: Mapping[str, Any] | ToolConfigSchema | None = None
) -> JsonObject:
    masked: JsonObject = dict(config)
    for key in get_sensitive_keys(config_schema):
        value = masked.get(key)
        if value and isinstance(value, str):
            suffix = value[-4:] if len(value) > 4 else value
            masked[key] = f"****{suffix}"
    return masked
