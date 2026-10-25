"""Canonical JSON document assembly; no application startup or infrastructure calls."""

import json
from collections.abc import Iterable
from copy import deepcopy

from pydantic import JsonValue, TypeAdapter

HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
DOCUMENT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def mapping(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object in contract document")
    return value


def references(value: JsonValue) -> set[str]:
    match value:
        case dict():
            result: set[str] = set()
            for key, child in value.items():
                if key == "$ref":
                    if not isinstance(child, str) or not child.startswith("#/components/schemas/"):
                        raise ValueError(f"Unsupported contract reference: {child!r}")
                    result.add(child.removeprefix("#/components/schemas/"))
                else:
                    result.update(references(child))
            return result
        case list():
            return set[str]().union(*(references(child) for child in value))
        case _:
            return set()


def assemble_contract(
    source: dict[str, JsonValue],
    audited_operations: Iterable[tuple[str, str]],
    explicit: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    document = deepcopy(source)
    paths = mapping(document["paths"])
    components = mapping(document["components"])
    schemas = mapping(components["schemas"])
    roots: set[str] = set()
    for path, method in audited_operations:
        roots.update(references(mapping(paths[path])[method]))
    for name, raw_schema in sorted(explicit.items()):
        schema = deepcopy(mapping(raw_schema))
        definitions = mapping(schema.pop("$defs", {}))
        for definition_name, definition in [*definitions.items(), (name, schema)]:
            if definition_name in schemas and schemas[definition_name] != definition:
                raise ValueError(f"Conflicting explicit schema: {definition_name}")
            schemas[definition_name] = definition
        roots.add(name)
    # Unique even for multi-method routes: the original OpenAPI cache stays untouched.
    for path, path_item in paths.items():
        for method, operation in mapping(path_item).items():
            if method in HTTP_METHODS:
                mapping(operation)["operationId"] = f"{method}_{path.encode('utf-8').hex()}"
    pending = list(roots)
    closure: set[str] = set()
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        if name not in schemas:
            raise ValueError(f"Unresolved schema: {name}")
        closure.add(name)
        pending.extend(references(schemas[name]) - closure)
    registry: dict[str, JsonValue] = {
        "version": 1,
        "roots": list[JsonValue](sorted(roots)),
        "schemas": list[JsonValue](sorted(closure)),
    }
    document["x-maraclaw-contract"] = registry
    return document


def canonical_json(document: dict[str, JsonValue]) -> str:
    return json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"
