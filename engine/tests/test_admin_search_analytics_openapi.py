import pytest
from fastapi import FastAPI

from app.api import admin_search_analytics as analytics
from app.core.json_types import JsonObject, json_as_str_or, json_object_from


@pytest.fixture
def schemas() -> dict[str, JsonObject]:
    app = FastAPI()
    app.include_router(analytics.router, prefix="/api")
    document = json_object_from(app.openapi())
    definitions = json_object_from(json_object_from(document["components"])["schemas"])
    result: dict[str, JsonObject] = {}
    for path, entry in json_object_from(document["paths"]).items():
        operation = json_object_from(json_object_from(entry)["get"])
        success = json_object_from(json_object_from(operation["responses"])["200"])
        schema = json_object_from(json_object_from(json_object_from(success["content"])["application/json"])["schema"])
        if schema.get("type") == "array":
            schema = json_object_from(schema["items"])
        reference = json_as_str_or(schema["$ref"])
        result[path.rsplit("/", 1)[-1]] = json_object_from(definitions[reference.rsplit("/", 1)[-1]])
    summary = result["summary"]
    items = json_object_from(json_object_from(json_object_from(summary["properties"])["by_kind"])["items"])
    reference = json_as_str_or(items["$ref"])
    result["kind-count"] = json_object_from(definitions[reference.rsplit("/", 1)[-1]])
    scope = json_object_from(json_object_from(summary["properties"])["scope"])
    reference = json_as_str_or(scope["$ref"])
    result["scope"] = json_object_from(definitions[reference.rsplit("/", 1)[-1]])
    return result


@pytest.mark.parametrize(
    ("path", "required"),
    [
        (
            "summary",
            {
                "scope",
                "event_count",
                "error_count",
                "quota_count",
                "unique_orgs",
                "unique_agents",
                "unattributed_count",
                "avg_latency_ms",
                "by_kind",
            },
        ),
        ("timeseries", {"date", "event_count", "error_count", "quota_count"}),
        ("orgs", {"tenant_id", "name", "event_count", "quota_count"}),
        ("trending", {"query_hash", "query_normalized", "kind", "hits", "distinct_agents", "distinct_orgs"}),
        (
            "export-status",
            {
                "pending",
                "exporting",
                "exported",
                "skipped",
                "last_exported_at",
                "export_enabled",
                "include_raw",
                "capture_enabled",
                "bucket",
                "prefix",
            },
        ),
    ],
)
def test_analytics_openapi_requires_complete_projections(
    schemas: dict[str, JsonObject], path: str, required: set[str]
) -> None:
    # Given the schemas attached to the actual public response operations.
    # When discovering the contract for a complete projection.
    schema = schemas[path]
    # Then every producer field is required and arbitrary extensions are not advertised.
    fields = schema["required"]
    assert isinstance(fields, list)
    assert set(fields) == required
    assert set(json_object_from(schema["properties"])) == required
    assert schema.get("additionalProperties") is not True


@pytest.mark.parametrize(("path", "field"), [("orgs", "tenant_id"), ("export-status", "bucket")])
def test_analytics_openapi_distinguishes_required_nullable_strings(
    schemas: dict[str, JsonObject], path: str, field: str
) -> None:
    # Given fields which clients receive even when their values are null.
    # When inspecting their field schemas.
    variants = json_object_from(json_object_from(schemas[path]["properties"])[field])["anyOf"]
    # Then nullability does not imply omission.
    assert variants == [{"type": "string"}, {"type": "null"}]


def test_analytics_openapi_describes_export_datetime_string_and_null(schemas: dict[str, JsonObject]) -> None:
    # Given the DAO's driver-dependent timestamp representation.
    # When inspecting the HTTP timestamp contract.
    field = json_object_from(json_object_from(schemas["export-status"]["properties"])["last_exported_at"])
    # Then datetime strings, opaque legacy strings and explicit null remain supported.
    assert field["anyOf"] == [{"type": "string", "format": "date-time"}, {"type": "string"}, {"type": "null"}]


@pytest.mark.parametrize(
    ("path", "types"),
    [
        ("summary", {"event_count": "integer", "avg_latency_ms": "number", "by_kind": "array"}),
        (
            "timeseries",
            {"date": "string", "event_count": "integer", "error_count": "integer", "quota_count": "integer"},
        ),
        ("orgs", {"name": "string", "event_count": "integer", "quota_count": "integer"}),
        (
            "trending",
            {
                "query_hash": "string",
                "query_normalized": "string",
                "kind": "string",
                "hits": "integer",
                "distinct_agents": "integer",
                "distinct_orgs": "integer",
            },
        ),
        (
            "export-status",
            {
                "pending": "integer",
                "exporting": "integer",
                "exported": "integer",
                "skipped": "integer",
                "export_enabled": "boolean",
                "include_raw": "boolean",
                "capture_enabled": "boolean",
                "prefix": "string",
            },
        ),
    ],
)
def test_analytics_openapi_describes_client_primitive_types(
    schemas: dict[str, JsonObject], path: str, types: dict[str, str]
) -> None:
    # Given the published response schema, rather than an unattached model.
    # When reading its client-facing fields.
    properties = json_object_from(schemas[path]["properties"])
    # Then numeric, textual, boolean and aggregation values have faithful types.
    assert {field: json_object_from(properties[field])["type"] for field in types} == types


def test_analytics_openapi_describes_summary_scope(schemas: dict[str, JsonObject]) -> None:
    # Given the scope definition referenced by the summary operation.
    # When inspecting the resolved contract.
    scope = schemas["scope"]
    # Then clients see exactly the existing system and organization scope values.
    assert scope["type"] == "string"
    assert scope["enum"] == ["org", "system"]


def test_analytics_openapi_describes_summary_aggregations(schemas: dict[str, JsonObject]) -> None:
    # Given a router with nested aggregation contracts resolved through their references.
    # When inspecting the nested kind-count response schema.
    nested = schemas["kind-count"]
    # Then both nested fields are required with the actual primitive types.
    fields = nested["required"]
    assert isinstance(fields, list)
    assert set(fields) == {"kind", "event_count"}
    properties = json_object_from(nested["properties"])
    assert json_object_from(properties["kind"])["type"] == "string"
    assert json_object_from(properties["event_count"])["type"] == "integer"
