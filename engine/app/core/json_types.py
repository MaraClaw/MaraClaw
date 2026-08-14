"""Shared JSON value annotations for persisted configuration and event data."""

from __future__ import annotations

from typing import Any, TypeIs

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


def json_as_str(value: object) -> str | None:
    """Return ``value`` when it is a string, otherwise ``None``."""
    return value if isinstance(value, str) else None


def json_as_str_or(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def json_as_int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def json_as_bool(value: object, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def is_json_value(value: object) -> TypeIs[JsonValue]:
    """Narrow a JSON-compatible scalar, list, or object."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(is_json_value(item) for item in list[object](value))
    return is_json_object(value)


def is_json_object(value: object) -> TypeIs[JsonObject]:
    """Narrow a mapping to ``JsonObject`` (HTTP JSON, JSONB objects)."""
    if not isinstance(value, dict):
        return False
    mapping: dict[object, object] = dict(value)
    return all(isinstance(key, str) and is_json_value(item) for key, item in mapping.items())


def json_object_from(value: object) -> JsonObject:
    """Return ``value`` when it is a JSON object, otherwise an empty dict."""
    return value if is_json_object(value) else {}


def is_str_dict(value: object) -> TypeIs[dict[str, Any]]:
    """Narrow a mapping to ``dict[str, Any]`` (JSONB objects, callback payloads)."""
    return isinstance(value, dict)


def is_any_list(value: object) -> TypeIs[list[Any]]:
    """Narrow a sequence to ``list[Any]``."""
    return isinstance(value, list)


def mapping_from_row(value: object) -> dict[str, Any]:
    """Return ``value`` when it is a mapping, otherwise an empty dict."""
    return value if is_str_dict(value) else {}


def str_list_from_row(value: object) -> list[str]:
    """Return string items from a JSON array, otherwise an empty list."""
    if not is_any_list(value):
        return []
    return [item for item in value if isinstance(item, str)]


def object_list_from_row(value: object) -> list[dict[str, Any]]:
    """Return mapping items from a JSON array, otherwise an empty list."""
    if not is_any_list(value):
        return []
    return [item for item in value if is_str_dict(item)]
