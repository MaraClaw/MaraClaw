"""Shared JSON value annotations for persisted configuration and event data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol, TypeIs
from uuid import UUID

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


class SupportsJson(Protocol):
    """HTTP-style payload that can be decoded as JSON."""

    def json(self) -> object: ...


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


def json_loads_object(value: str | bytes | bytearray) -> JsonObject:
    """Parse JSON text and return an object, or ``{}`` when the payload is not a mapping."""
    return json_object_from(json_loads_value(value))


def json_loads_value(value: str | bytes | bytearray) -> object:
    """Parse JSON text and return the decoded value."""
    import json

    return object_call(json.loads, value)


def json_object_from_response(response: SupportsJson) -> JsonObject:
    """Decode an HTTP JSON body when it is an object, otherwise ``{}``."""
    return json_object_from(response.json())


def json_value_from_response(response: SupportsJson) -> object:
    """Decode an HTTP JSON body without assuming an object payload."""
    return response.json()


def object_from_literal(value: str) -> object:
    """Decode a JSON document or Python literal as ``object``."""
    import ast

    try:
        return object_call(ast.literal_eval, value)
    except ValueError, SyntaxError:
        loaded = json_loads_value(value)
        return loaded if is_json_value(loaded) else None


def is_str_dict(value: object) -> TypeIs[dict[str, Any]]:
    """Narrow a mapping to ``dict[str, Any]`` (JSONB objects, callback payloads)."""
    return isinstance(value, dict)


def is_any_list(value: object) -> TypeIs[list[Any]]:
    """Narrow a sequence to ``list[Any]``."""
    return isinstance(value, list)


def mapping_from_row(value: object) -> dict[str, Any]:
    """Return ``value`` when it is a mapping, otherwise an empty dict."""
    return value if is_str_dict(value) else {}


def object_mapping_from(value: object) -> dict[str, object]:
    """Return a ``dict[str, object]`` copy of a mapping, otherwise ``{}``."""
    if not isinstance(value, dict):
        return {}
    mapping: dict[object, object] = dict(value)
    return {str(key): item for key, item in mapping.items()}


def str_list_from_row(value: object) -> list[str]:
    """Return string items from a JSON array, otherwise an empty list."""
    if not isinstance(value, list):
        return []
    items = list[object](value)
    return [item for item in items if isinstance(item, str)]


def object_list_from_row(value: object) -> list[dict[str, Any]]:
    """Return mapping items from a JSON array, otherwise an empty list."""
    if not isinstance(value, list):
        return []
    items = list[object](value)
    return [item for item in items if is_str_dict(item)]


def uuid_from_row(value: object) -> UUID:
    """Require a UUID-ish cell from a DictRow."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def uuid_from_row_opt(value: object) -> UUID | None:
    return None if value is None else uuid_from_row(value)


def datetime_from_row(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def date_from_row(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"expected date, got {type(value)!r}")


def date_from_row_opt(value: object) -> date | None:
    return None if value is None else date_from_row(value)


def object_attr(value: object, name: str, default: object = None) -> object:
    """Read an attribute without leaking ``Any`` from ``getattr``."""
    from typing import cast

    return cast(object, getattr(value, name, default))


def object_call(fn: object, *args: object) -> object:
    """Call ``fn`` when it is callable and return the result as ``object``."""
    if not callable(fn):
        raise TypeError(f"expected callable, got {type(fn)!r}")
    return fn(*args)


def http_header(headers: object, key: str, default: str = "") -> str:
    """Read an HTTP header as ``str`` without leaking decoder ``Any``."""
    getter = object_attr(headers, "get")
    value = object_call(getter, key) if callable(getter) else None
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def yaml_load_object(value: str) -> object:
    """Parse a YAML document and return the decoded value as ``object``."""
    import yaml

    return object_call(yaml.safe_load, value)


def str_findall(pattern: object, text: str) -> list[str]:
    """Return string matches from a compiled regex ``findall``."""
    findall = object_attr(pattern, "findall")
    if not callable(findall):
        return []
    found: object = findall(text)
    if not isinstance(found, list):
        return []
    return [item for item in list[object](found) if isinstance(item, str)]


def str_from_row(value: object, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default if value is None else str(value)


def str_from_row_opt(value: object) -> str | None:
    return None if value is None else str_from_row(value)


def float_from_row(value: object, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def uuid_list_from_rows(rows: list[dict[str, object]], key: str = "id") -> list[UUID]:
    return [uuid_from_row(row[key]) for row in rows]


def int_from_row(value: object, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default
