import httpx
import pytest
from fastapi import FastAPI

from app.api import tools
from app.core.json_types import JsonObject, json_as_str_or, json_object_from, json_object_from_response


@pytest.fixture
def tool_app() -> FastAPI:
    app = FastAPI()
    app.include_router(tools.router, prefix="/api")
    return app


def _resolve(schema: JsonObject, schemas: JsonObject) -> JsonObject:
    reference = schema.get("$ref")
    return json_object_from(schemas[reference.rsplit("/", 1)[-1]]) if isinstance(reference, str) else schema


def _variant_refs(schema: JsonObject, schemas: JsonObject) -> set[str]:
    variants = _resolve(schema, schemas).get("anyOf")
    if isinstance(variants, list):
        return {reference for variant in variants for reference in _variant_refs(json_object_from(variant), schemas)}
    return {json_as_str_or(schema.get("$ref"))}


@pytest.mark.parametrize(
    ("path", "method", "container", "contract"),
    [
        ("", "get", "items", "ToolCatalogResponse"),
        ("", "post", None, "ToolCreatedResponse"),
        ("/bulk", "put", None, "ToolAcknowledgement"),
        ("/mcp-server", "put", None, "MCPServerUpdatedResponse"),
        ("/{tool_id}", "put", None, "ToolAcknowledgement"),
        ("/{tool_id}", "delete", None, "ToolAcknowledgement"),
        ("/agents/{agent_id}", "get", "items", "AgentToolResponse"),
        ("/agents/{agent_id}", "put", None, "ToolAcknowledgement"),
        ("/agent-installed", "get", "items", "AgentInstalledToolResponse"),
        ("/agent-tool/{agent_tool_id}", "delete", None, "ToolAcknowledgement"),
        ("/agents/{agent_id}/tool-config/{tool_id}", "get", None, "AgentToolConfigResponse"),
        ("/agents/{agent_id}/tool-config/{tool_id}", "put", None, "ToolAcknowledgement"),
        ("/agents/{agent_id}/with-config", "get", "items", "AgentToolWithConfigResponse"),
        ("/email-providers", "get", "additionalProperties", "EmailProviderHelp"),
        ("/agents/{agent_id}/category-config/{category}", "get", None, "CategoryConfigResponse"),
        ("/agents/{agent_id}/category-config/{category}", "post", None, "ToolAcknowledgement"),
    ],
)
async def test_tool_success_schema_exposes_concrete_contract(
    tool_app: FastAPI, path: str, method: str, container: str | None, contract: str
) -> None:
    # Given the real router with its normal response serializer.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=tool_app), base_url="http://test") as client:
        # When clients discover the HTTP response contract.
        response = await client.get("/openapi.json")
    # Then every audited success body references its concrete object contract.
    document = json_object_from_response(response)
    paths = json_object_from(document["paths"])
    operation = json_object_from(json_object_from(paths[f"/api/tools{path}"])[method])
    success = json_object_from(json_object_from(operation["responses"])["200"])
    schema = json_object_from(json_object_from(json_object_from(success["content"])["application/json"])["schema"])
    if container is not None:
        schema = json_object_from(schema[container])
    assert schema["$ref"] == f"#/components/schemas/{contract}"


@pytest.mark.parametrize(
    ("path", "contracts"),
    [
        ("/test-mcp", {"MCPTestSuccess", "ToolTestFailure"}),
        (
            "/agents/{agent_id}/category-config/{category}/test",
            {"AtlassianTestSuccess", "ToolTestFailure", "CategoryTestSuccess"},
        ),
    ],
)
def test_tool_probe_schema_preserves_success_and_failure_variants(
    tool_app: FastAPI, path: str, contracts: set[str]
) -> None:
    # Given the probe endpoints that return success or failure bodies at HTTP 200.
    document = json_object_from(tool_app.openapi())
    paths = json_object_from(document["paths"])
    operation = json_object_from(json_object_from(paths[f"/api/tools{path}"])["post"])
    success = json_object_from(json_object_from(operation["responses"])["200"])
    # When inspecting the emitted response union.
    schema = json_object_from(json_object_from(json_object_from(success["content"])["application/json"])["schema"])
    schemas = json_object_from(json_object_from(document["components"])["schemas"])
    schema = _resolve(schema, schemas)
    variants = schema.get("anyOf")
    # Then both the discriminator and variant-specific fields are discoverable.
    assert isinstance(variants, list)
    assert _variant_refs(schema, schemas) == {f"#/components/schemas/{contract}" for contract in contracts}


@pytest.mark.parametrize(
    ("contract", "field"),
    [
        ("ToolCatalogResponse", "config"),
        ("ToolCatalogResponse", "config_schema"),
        ("ToolCatalogResponse", "parameters_schema"),
        ("AgentToolConfigResponse", "agent_config"),
        ("CategoryConfigResponse", "config"),
    ],
)
def test_tool_provider_owned_objects_remain_open(tool_app: FastAPI, contract: str, field: str) -> None:
    # Given provider-defined JSON objects inside concrete envelopes.
    document = json_object_from(tool_app.openapi())
    # When resolving their emitted property schemas.
    schemas = json_object_from(json_object_from(document["components"])["schemas"])
    properties = json_object_from(json_object_from(schemas[contract])["properties"])
    schema = _resolve(json_object_from(properties[field]), schemas)
    # Then response validation must not turn provider dictionaries into closed DTOs.
    assert schema["type"] == "object"
    assert isinstance(schema["additionalProperties"], dict)


def test_category_delete_schema_has_no_response_body(tool_app: FastAPI) -> None:
    # Given the bodyless category deletion endpoint.
    paths: JsonObject = json_object_from(json_object_from(tool_app.openapi())["paths"])
    operation = json_object_from(
        json_object_from(paths["/api/tools/agents/{agent_id}/category-config/{category}"])["delete"]
    )
    # When inspecting its success response.
    success = json_object_from(json_object_from(operation["responses"])["204"])
    # Then no JSON body (including JSON null) is advertised.
    assert "content" not in success
