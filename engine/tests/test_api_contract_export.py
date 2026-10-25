import socket
from pathlib import Path
from typing import NoReturn

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, JsonValue
from scripts.api_contract_document import assemble_contract, canonical_json, mapping
from scripts.api_contract_registry import ENUM_TYPES, explicit_schemas
from scripts.export_api_contracts import export_contract, write_or_check

from app.schemas.chat_wire import ChatEventType


def test_contract_when_operations_share_ids_preserves_source_and_normalizes_export() -> None:
    # Given
    source: dict[str, JsonValue] = {
        "paths": {"/proxy/{path}": {"get": {"operationId": "duplicate"}, "post": {"operationId": "duplicate"}}},
        "components": {"schemas": {}},
    }
    # When
    result = assemble_contract(source, [], {})
    # Then
    operations = mapping(mapping(result["paths"])["/proxy/{path}"])
    assert mapping(operations["get"])["operationId"] != mapping(operations["post"])["operationId"]
    assert mapping(mapping(mapping(source["paths"])["/proxy/{path}"])["get"])["operationId"] == "duplicate"


def test_contract_when_schema_has_references_limits_registry_to_transitive_closure() -> None:
    # Given
    source: dict[str, JsonValue] = {
        "paths": {"/items": {"get": {"responses": {"200": {"$ref": "#/components/schemas/Item"}}}}},
        "components": {
            "schemas": {
                "Item": {"type": "object", "properties": {"kind": {"$ref": "#/components/schemas/Kind"}}},
                "Kind": {"enum": ["one", "two"], "type": "string"},
                "Unrelated": {"type": "string"},
            }
        },
    }
    # When
    result = assemble_contract(source, [("/items", "get")], {})
    # Then
    assert mapping(result["x-maraclaw-contract"])["schemas"] == ["Item", "Kind"]
    assert "Unrelated" in mapping(mapping(result["components"])["schemas"])


def test_contract_when_explicit_schemas_conflict_rejects_overwrite() -> None:
    # Given
    source: dict[str, JsonValue] = {"paths": {}, "components": {"schemas": {"Kind": {"type": "string"}}}}
    # When / Then
    with pytest.raises(ValueError, match="Conflicting explicit schema"):
        assemble_contract(source, [], {"Kind": {"type": "integer"}})


def test_contract_when_reference_is_missing_rejects_export() -> None:
    # Given
    source: dict[str, JsonValue] = {"paths": {}, "components": {"schemas": {}}}
    # When / Then
    with pytest.raises(ValueError, match="Unresolved schema"):
        assemble_contract(source, [], {"Item": {"$ref": "#/components/schemas/Missing"}})


def test_explicit_registry_when_exported_uses_concrete_enum_values_and_ws_definitions() -> None:
    # Given / When
    schemas = explicit_schemas()
    # Then
    for enum_type in ENUM_TYPES:
        assert mapping(schemas[enum_type.__name__])["enum"] == [member.value for member in enum_type]
    assert "ConnectedEvent" in mapping(mapping(schemas["ChatServerEvent"])["$defs"])


class ContractItem(BaseModel):
    required_nullable: str | None
    optional_nullable: str | None = None


def test_export_when_route_is_audited_uses_real_openapi_requiredness() -> None:
    # Given
    application = FastAPI()

    def endpoint() -> ContractItem:
        return ContractItem(required_nullable=None)

    endpoint.__module__ = "app.api.agents"
    application.get("/contract", response_model=ContractItem)(endpoint)
    # When
    result = export_contract(application)
    # Then
    item = mapping(mapping(mapping(result["components"])["schemas"])["ContractItem"])
    assert item["required"] == ["required_nullable"]
    roots = mapping(result["x-maraclaw-contract"])["roots"]
    assert isinstance(roots, list)
    assert "ContractItem" in roots


def test_export_when_enum_is_shared_with_openapi_retains_ordered_members_without_mutating_cache() -> None:
    # Given a real route sharing the explicit enum schema.
    application = FastAPI()

    def endpoint() -> ChatEventType:
        return ChatEventType.CONNECTED

    endpoint.__module__ = "app.api.agents"
    application.get("/contract/event", response_model=ChatEventType)(endpoint)
    source = application.openapi()
    # When the offline exporter assembles its full contract.
    result = export_contract(application)
    # Then every registered enum carries positional Python member names and unchanged JSON values.
    schemas = mapping(mapping(result["components"])["schemas"])
    for enum_type in ENUM_TYPES:
        schema = mapping(schemas[enum_type.__name__])
        assert schema["x-enum-varnames"] == [member.name for member in enum_type]
        assert schema["enum"] == [member.value for member in enum_type]
    assert "x-enum-varnames" not in source["components"]["schemas"]["ChatEventType"]


def test_real_application_when_exported_never_connects_or_enters_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    def deny_connection(_socket: socket.socket, _address: str | tuple[str, int]) -> NoReturn:
        raise AssertionError("Contract export attempted network access")

    def deny_lifespan(_application: FastAPI) -> NoReturn:
        raise AssertionError("Contract export entered application lifespan")

    monkeypatch.setattr(socket.socket, "connect", deny_connection)
    from app.main import app

    monkeypatch.setattr(app.router, "lifespan_context", deny_lifespan)
    # When
    document = export_contract(app)
    # Then
    assert len(mapping(document["paths"])) > 300


def test_real_export_when_json_aliases_collide_names_each_definition_without_changing_source() -> None:
    # Given
    from app.main import app

    source = app.openapi()
    # When
    document = export_contract(app)
    # Then metadata names separate definitions; it does not rewrite/merge their schema or references.
    names = mapping(mapping(document["x-maraclaw-contract"])["names"])
    assert names == {
        "JsonValue": "ToolCallValue",
        "pydantic__types__JsonValue": "JsonValue",
        "app__core__json_types__JsonValue-Output": "JsonOutputValue",
        "JsonValue-Input": "JsonInputValue",
        "JsonObject-Input": "JsonInputObject",
        "JsonObject-Output": "JsonOutputObject",
    }
    schemas = mapping(mapping(document["components"])["schemas"])
    expected = {**source["components"]["schemas"], **mapping(mapping(explicit_schemas()["ChatServerEvent"])["$defs"])}
    for name in names:
        assert schemas[name] == expected[name]
    assert "x-maraclaw-contract" not in source


def test_canonical_json_when_mapping_insertion_order_changes_is_identical() -> None:
    # Given
    left: dict[str, JsonValue] = {"b": {"d": 2, "c": 1}, "a": [1, None]}
    right: dict[str, JsonValue] = {"a": [1, None], "b": {"c": 1, "d": 2}}
    # When / Then
    assert canonical_json(left) == canonical_json(right)


@pytest.mark.parametrize("existing", [None, "old\n"])
def test_check_when_output_is_missing_or_stale_never_writes(tmp_path: Path, existing: str | None) -> None:
    # Given
    output = tmp_path / "api-contract.json"
    if existing is not None:
        output.write_text(existing)
    # When
    matches = write_or_check(output, "new\n", check=True)
    # Then
    assert matches is False
    assert (output.read_text() if output.exists() else None) == existing


def test_check_when_output_is_current_succeeds(tmp_path: Path) -> None:
    # Given
    output = tmp_path / "api-contract.json"
    output.write_text("current\n")
    # When / Then
    assert write_or_check(output, "current\n", check=True)
