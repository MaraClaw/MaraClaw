from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter, ValidationError
from tests.analytics_response_fixtures import (
    export_status_projection,
    org_ranking_projection,
    summary_projection,
    time_bucket_projection,
    trending_query_projection,
)
from typing_extensions import TypeForm

from app.api import admin_search_analytics as analytics_api, atlassian as atlassian_api, tools as tools_api
from app.config import Settings
from app.core.domain.channels import ChannelType
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject, json_object_from
from app.core.security import encrypt_data, get_current_user
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.tool import AgentToolRecord, ToolRecord
from app.records.user import UserRecord
from app.schemas import operator_response_analytics as analytics_contracts, operator_response_tools as tool_contracts
from app.services import email_service, mcp_client, tool_config

type ToolState = tuple[UserRecord, AgentRecord, ToolRecord, AgentToolRecord]


@pytest.fixture
def tool_state(monkeypatch: pytest.MonkeyPatch) -> ToolState:
    user = UserRecord(id=uuid4(), tenant_id=uuid4(), role=UserRole.ORG_ADMIN)
    agent = AgentRecord(id=uuid4(), name="Operator", creator_id=user.id, tenant_id=user.tenant_id)
    settings = Settings(SECRET_KEY="operator-response-contract-test-key")
    monkeypatch.setattr(tool_config, "get_settings", lambda: settings)
    tool = ToolRecord(
        id=uuid4(),
        name="lookup",
        display_name="Lookup",
        source="builtin",
        parameters_schema={"vendor": {"nullable": None, "choices": [False, 3]}},
        config_schema={"fields": [{"key": "credential", "type": "password"}], "vendor": {"flag": False}},
    )
    assignment = AgentToolRecord(
        id=uuid4(),
        agent_id=agent.id,
        tool_id=tool.id,
        enabled=False,
        config={"credential": encrypt_data("agent-secret", settings.SECRET_KEY), "optional": None},
    )

    async def access(*_args: object) -> tuple[AgentRecord, str]:
        return agent, "manage"

    async def load(*_args: object) -> AgentRecord:
        return agent

    async def get_tool(*_args: object) -> ToolRecord:
        return tool

    async def has_feishu(*_args: object) -> bool:
        return False

    async def catalog(*_args: object, **_kwargs: object) -> list[ToolRecord]:
        return [tool]

    async def assignments(*_args: object) -> list[AgentToolRecord]:
        return [assignment]

    async def assigned(*_args: object) -> AgentToolRecord:
        return assignment

    async def company(*_args: object) -> JsonObject:
        return {"credential": "company-secret", "nested": {"nullable": None, "enabled": False}}

    async def builtin(*_args: object) -> dict[str, JsonObject]:
        return {tool.name: await company()}

    monkeypatch.setattr(tools_api, "check_agent_access", access)
    monkeypatch.setattr("app.core.permissions.check_agent_access", access)
    monkeypatch.setattr(tools_api.agent_dao, "get", load)
    monkeypatch.setattr(tools_api.tool_dao, "get", get_tool)
    monkeypatch.setattr(tools_api.tool_dao, "list_platform_for_tenant", catalog)
    monkeypatch.setattr(tools_api.tool_dao, "list_enabled_visible", catalog)
    monkeypatch.setattr(tools_api.tool_dao, "list_enabled_by_category_visible", catalog)
    monkeypatch.setattr(tools_api.agent_tool_dao, "list_for_agent", assignments)
    monkeypatch.setattr(tools_api.agent_tool_dao, "get_assignment", assigned)
    monkeypatch.setattr(tools_api, "get_tool_company_config", company)
    monkeypatch.setattr(tools_api, "get_tenant_tool_configs", builtin)
    monkeypatch.setattr("app.services.agent_tools._agent_has_feishu", has_feishu)
    return user, agent, tool, assignment


async def test_catalog_keeps_dynamic_schemas_and_masked_company_config(tool_state: ToolState) -> None:
    user, _agent, tool, _assignment = tool_state

    result = await tools_api.list_tools(current_user=user)

    assert result[0]["config"] == {
        "credential": "****cret",
        "nested": {"nullable": None, "enabled": False},
    }
    assert result[0]["parameters_schema"] == tool.parameters_schema
    assert result[0]["config_schema"] == tool.config_schema
    assert result[0]["created_at"] is None
    assert result[0]["mcp_tool_name"] is None
    assert TypeAdapter(list[tool_contracts.ToolCatalogResponse]).validate_python(result) == result


async def test_assignment_projection_keeps_disabled_assignment_and_omits_config(tool_state: ToolState) -> None:
    user, agent, _tool, _assignment = tool_state

    result = await tools_api.get_agent_tools(agent.id, current_user=user)

    assert result[0]["enabled"] is False
    assert "config" not in result[0]
    assert "agent_tool_id" not in result[0]
    assert TypeAdapter(list[tool_contracts.AgentToolResponse]).validate_python(result) == result


async def test_tool_config_masks_both_configs_without_dropping_null(tool_state: ToolState) -> None:
    user, agent, tool, _assignment = tool_state

    result = await tools_api.get_agent_tool_config(agent.id, tool.id, current_user=user)

    assert result["agent_config"] == {"credential": "****cret", "optional": None}
    assert json_object_from(result["global_config"])["credential"] == "****cret"
    assert json_object_from(result["merged_config"])["optional"] is None
    assert TypeAdapter(tool_contracts.AgentToolConfigResponse).validate_python(result) == result


async def test_with_config_preserves_existing_unmasked_agent_override(tool_state: ToolState) -> None:
    user, agent, _tool, assignment = tool_state

    result = await tools_api.get_agent_tools_with_config(agent.id, current_user=user)

    assert result[0]["agent_config"] == {"credential": "agent-secret", "optional": None}
    assert json_object_from(result[0]["global_config"])["credential"] == "****cret"
    assert result[0]["agent_tool_id"] == str(assignment.id)
    assert TypeAdapter(list[tool_contracts.AgentToolWithConfigResponse]).validate_python(result) == result


@pytest.mark.parametrize("failure", [False, True])
async def test_mcp_wire_preserves_dynamic_provider_tools_and_failure_variant(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    provider_tools: list[JsonObject] = [{"name": "lookup", "inputSchema": {"vendor": [None, False]}}]

    async def list_tools(_self: mcp_client.MCPClient) -> list[JsonObject]:
        if failure:
            raise ValueError("x" * 350)
        return provider_tools

    monkeypatch.setattr(mcp_client.MCPClient, "list_tools", list_tools)
    app = FastAPI()
    app.include_router(tools_api.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4())

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/tools/test-mcp", json={"server_url": "https://8.8.8.8/mcp"})

    assert response.status_code == 200
    assert response.json() == ({"ok": False, "error": "x" * 300} if failure else {"ok": True, "tools": provider_tools})
    assert TypeAdapter(tool_contracts.MCPTestResponse).validate_json(response.content) == response.json()


async def test_atlassian_serializer_preserves_dynamic_cloud_id_without_secret() -> None:
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=uuid4(),
        channel_type=ChannelType.ATLASSIAN,
        app_secret="never-return-this",
        extra_config={"cloud_id": None, "vendor": {"enabled": False}},
    )

    result = atlassian_api._serialize(config)

    assert result["cloud_id"] is None
    assert result["extra_config"] == {"cloud_id": None, "vendor": {"enabled": False}}
    assert result["created_at"] is None
    assert "app_secret" not in result
    assert TypeAdapter(tool_contracts.AtlassianChannelResponse).validate_python(result) == result


@pytest.mark.parametrize("operation", ["create", "update", "get", "delete"])
async def test_atlassian_config_wire_keeps_redacted_envelope_and_empty_delete(
    monkeypatch: pytest.MonkeyPatch, tool_state: ToolState, operation: str
) -> None:
    user, agent, _tool, _assignment = tool_state
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent.id,
        channel_type=ChannelType.ATLASSIAN,
        app_secret="never-return-this",
        extra_config={"cloud_id": None, "vendor": False},
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
    )

    async def channel(**_kwargs: object) -> ChannelConfigRecord | None:
        return None if operation == "create" else config

    async def save(**_kwargs: object) -> ChannelConfigRecord:
        return config

    async def remove(**_kwargs: object) -> None:
        return None

    def schedule(coroutine: Coroutine[object, object, object], _label: str) -> None:
        coroutine.close()

    monkeypatch.setattr(atlassian_api, "check_agent_access", tools_api.check_agent_access)
    monkeypatch.setattr(atlassian_api.channel_config_dao, "get_for_agent", channel)
    monkeypatch.setattr(atlassian_api.channel_config_dao, "create", save)
    monkeypatch.setattr(atlassian_api.channel_config_dao, "update", save)
    monkeypatch.setattr(atlassian_api.channel_config_dao, "delete", remove)
    monkeypatch.setattr("app.api.background_tasks.schedule_background_task", schedule)
    app = FastAPI()
    app.include_router(atlassian_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    method = {"create": "POST", "update": "POST", "get": "GET", "delete": "DELETE"}[operation]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            method, f"/agents/{agent.id}/atlassian-channel", json={"api_key": "request-secret"}
        )

    if operation == "delete":
        assert response.status_code == 204
        assert response.content == b""
    else:
        assert response.status_code == (201 if method == "POST" else 200)
        body = TypeAdapter(tool_contracts.AtlassianChannelResponse).validate_json(response.content)
        assert body["cloud_id"] is None
        assert body["extra_config"] == {"cloud_id": None, "vendor": False}
        assert body["created_at"] == "2026-10-20T00:00:00+00:00"
        assert "never-return-this" not in response.text
        assert "request-secret" not in response.text


@pytest.mark.parametrize("tenant_id", [None, UUID("00000000-0000-0000-0000-000000000001")])
async def test_analytics_summary_keeps_complete_projection_identity_and_scope(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID | None
) -> None:
    payload = summary_projection()

    async def summary(**_kwargs: object) -> analytics_contracts.SearchAnalyticsSummaryProjection:
        return payload

    monkeypatch.setattr(analytics_api.web_search_event_dao, "summary", summary)

    result = await analytics_api.get_search_analytics_summary(
        start=None, end=None, tenant_id=tenant_id, current_user=UserRecord(id=uuid4())
    )

    assert result is payload
    assert result == {**summary_projection(), "scope": "org" if tenant_id else "system"}


async def test_export_status_wire_keeps_datetime_encoding_and_complete_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exported_at = datetime(2026, 10, 20, 1, 2, 3, tzinfo=UTC)
    payload = export_status_projection(exported_at)

    async def status() -> analytics_contracts.SearchAnalyticsExportStatusProjection:
        return payload

    monkeypatch.setattr(analytics_api.web_search_event_dao, "export_status", status)
    monkeypatch.setattr(analytics_api, "analytics_bucket", lambda: "")
    monkeypatch.setattr(analytics_api, "analytics_prefix", lambda: "web-search")
    app = FastAPI()
    app.include_router(analytics_api.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/admin/search-analytics/export-status")

    body = TypeAdapter(JsonObject).validate_json(response.content)
    assert response.status_code == 200
    assert body["last_exported_at"] == "2026-10-20T01:02:03Z"
    assert body["bucket"] is None
    assert body["exported"] == 2


async def test_email_test_keeps_optional_fields_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def probe(_config: email_service.EmailConfig) -> email_service.EmailConnectionResult:
        return {"ok": True, "imap": "connected"}

    monkeypatch.setattr(email_service, "test_connection", probe)

    result = await tools_api.check_email_connection(tools_api.EmailTestRequest(config={}))

    assert result == {"ok": True, "imap": "connected"}
    assert TypeAdapter(email_service.EmailConnectionResult).validate_python(result) == result


async def test_installed_tools_normalize_nullable_metadata_and_keep_configuration_flag(
    monkeypatch: pytest.MonkeyPatch, tool_state: ToolState
) -> None:
    user, agent, tool, assignment = tool_state
    rows: list[dict[str, object]] = [
        {
            "agent_tool_id": assignment.id,
            "agent_id": agent.id,
            "tool_id": tool.id,
            "enabled": False,
            "config": {"optional": None},
            "mcp_server_name": "",
            "installed_at": datetime(2026, 10, 20, tzinfo=UTC),
        }
    ]

    async def installed(_tenant_id: str | None) -> list[dict[str, object]]:
        return rows

    monkeypatch.setattr(tools_api.agent_tool_dao, "list_agent_installed", installed)

    result = await tools_api.list_agent_installed_tools(current_user=user)

    assert result[0]["configured"] is True
    assert result[0]["enabled"] is False
    assert result[0]["mcp_server_name"] is None
    assert result[0]["installed_by_agent_id"] is None
    assert result[0]["installed_at"] == "2026-10-20T00:00:00+00:00"
    assert TypeAdapter(list[tool_contracts.AgentInstalledToolResponse]).validate_python(result) == result


async def test_category_config_preserves_agent_overrides_and_masked_global_config(
    monkeypatch: pytest.MonkeyPatch, tool_state: ToolState
) -> None:
    user, agent, _tool, _assignment = tool_state
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent.id,
        channel_type=ChannelType.SLACK,
        app_secret=encrypt_data("agent-key", tool_config.get_settings().SECRET_KEY),
        extra_config={"credential": "agent-override", "optional": None, "vendor": False},
    )

    async def channel(*, agent_id: UUID, channel_type: ChannelType) -> ChannelConfigRecord | None:
        return config if agent_id == config.agent_id and channel_type.value == config.channel_type.value else None

    monkeypatch.setattr(tools_api.channel_config_dao, "get_for_agent", channel)

    result = await tools_api.get_category_config(agent.id, ChannelType.SLACK.value, current_user=user)

    assert result["config"]["credential"] == "agent-override"
    assert result["global_config"]["credential"] == "****cret"
    assert result["agent_config"]["api_key"] == "agent-key"
    assert result["agent_config"]["vendor"] is False
    assert "optional" not in result["agent_config"]
    assert TypeAdapter(tool_contracts.CategoryConfigResponse).validate_python(result) == result


async def test_email_provider_help_exposes_only_public_help_fields() -> None:
    result = await tools_api.get_email_providers()

    assert result
    assert all(set(help_fields) == {"label", "help_url", "help_text"} for help_fields in result.values())
    assert TypeAdapter(dict[str, tool_contracts.EmailProviderHelp]).validate_python(result) == result


@pytest.mark.parametrize("failure", [False, True])
async def test_atlassian_test_wire_keeps_preview_limit_dynamic_name_and_failure_variant(
    monkeypatch: pytest.MonkeyPatch, tool_state: ToolState, failure: bool
) -> None:
    user, agent, _tool, _assignment = tool_state
    config = ChannelConfigRecord(id=uuid4(), agent_id=agent.id, app_secret="never-return-this")

    async def channel(**_kwargs: object) -> ChannelConfigRecord:
        return config

    async def list_tools(_self: mcp_client.MCPClient) -> list[JsonObject]:
        if failure:
            raise ValueError("x" * 350)
        return [{"name": None, "description": "x" * 150} for _index in range(12)]

    monkeypatch.setattr(atlassian_api, "check_agent_access", tools_api.check_agent_access)
    monkeypatch.setattr(atlassian_api.channel_config_dao, "get_for_agent", channel)
    monkeypatch.setattr(mcp_client.MCPClient, "list_tools", list_tools)
    app = FastAPI()
    app.include_router(atlassian_api.router)
    app.dependency_overrides[get_current_user] = lambda: user

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/agents/{agent.id}/atlassian-channel/test")

    body = TypeAdapter(tool_contracts.AtlassianTestResponse).validate_json(response.content)
    assert response.status_code == 200
    if failure:
        assert body == {"ok": False, "error": "x" * 300}
    else:
        success = TypeAdapter(tool_contracts.AtlassianTestSuccess).validate_json(response.content)
        assert success["tool_count"] == 12
        assert success["tools"] == [{"name": None, "description": "x" * 100} for _index in range(10)]
    assert "never-return-this" not in response.text


async def test_category_test_preserves_agentbay_dictionary_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import agentbay_client

    payload = {"ok": True, "message": "connected", "extra": False}

    async def check(_agent_id: UUID, _current_user: UserRecord) -> dict[str, bool | str]:
        return payload

    monkeypatch.setattr(agentbay_client, "check_agentbay_channel", check)

    result = await tools_api.check_category_config(uuid4(), "agentbay", UserRecord(id=uuid4()))

    assert result is payload


async def test_category_test_fallback_matches_success_contract() -> None:
    result = await tools_api.check_category_config(uuid4(), "custom", UserRecord(id=uuid4()))

    assert result["ok"] is True
    assert TypeAdapter(tool_contracts.CategoryTestResponse).validate_python(result) == result


@pytest.mark.parametrize(
    ("path", "producer", "payload"),
    [
        ("timeseries", "timeseries", [time_bucket_projection()]),
        ("orgs", "top_orgs", [org_ranking_projection()]),
        ("trending", "trending", [trending_query_projection()]),
    ],
)
async def test_analytics_list_wire_keeps_complete_projection_values(
    monkeypatch: pytest.MonkeyPatch, path: str, producer: str, payload: list[JsonObject]
) -> None:
    async def rows(**_kwargs: object) -> list[JsonObject]:
        return payload

    monkeypatch.setattr(analytics_api.web_search_event_dao, producer, rows)
    app = FastAPI()
    app.include_router(analytics_api.router)
    app.dependency_overrides[get_current_user] = lambda: UserRecord(id=uuid4(), role=UserRole.PLATFORM_ADMIN)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/admin/search-analytics/{path}")

    assert response.status_code == 200
    assert response.json() == payload


@pytest.mark.parametrize(
    ("contract", "payload"),
    [
        (tool_contracts.ToolAcknowledgement, {"ok": False}),
        (tool_contracts.ToolCreatedResponse, {"id": "id"}),
        (tool_contracts.MCPServerUpdatedResponse, {"ok": True, "updated": None}),
        (tool_contracts.MCPTestResponse, {"ok": True, "error": "failed"}),
        (tool_contracts.AtlassianTestResponse, {"ok": True, "message": "connected"}),
        (analytics_contracts.SearchAnalyticsSummary, {"event_count": 2, "by_kind": [], "scope": "system"}),
        (analytics_contracts.SearchAnalyticsTimeBucket, {"date": None, "event_count": 2}),
        (analytics_contracts.SearchAnalyticsOrgRanking, {"tenant_id": None, "name": "org"}),
        (analytics_contracts.SearchAnalyticsTrendingQuery, {"query_normalized": "query", "hits": 2}),
        (analytics_contracts.SearchAnalyticsExportStatus, {"pending": 1, "last_exported_at": None}),
    ],
)
def test_fixed_contracts_reject_missing_required_fields_or_wrong_variants(
    contract: TypeForm[object], payload: JsonObject
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter[object](contract).validate_python(payload)


def test_complete_analytics_contracts_support_pydantic_json_serialization() -> None:
    summary: analytics_contracts.SearchAnalyticsSummary = {
        **summary_projection(),
        "scope": "org",
    }
    status: analytics_contracts.SearchAnalyticsExportStatus = {
        "pending": 1,
        "exporting": 0,
        "exported": 2,
        "skipped": 0,
        "last_exported_at": datetime(2026, 10, 20, tzinfo=UTC),
        "export_enabled": False,
        "include_raw": False,
        "capture_enabled": True,
        "bucket": None,
        "prefix": "web-search",
    }

    assert TypeAdapter(analytics_contracts.SearchAnalyticsSummary).validate_python(summary) == summary
    assert TypeAdapter(JsonObject).validate_json(
        TypeAdapter(analytics_contracts.SearchAnalyticsExportStatus).dump_json(status)
    ) == {**status, "last_exported_at": "2026-10-20T00:00:00Z"}
    assert TypeAdapter(list[analytics_contracts.SearchAnalyticsTimeBucket]).validate_python(
        [{"date": "2026-10-20", "event_count": 2, "error_count": 0, "quota_count": 1}]
    ) == [{"date": "2026-10-20", "event_count": 2, "error_count": 0, "quota_count": 1}]
    assert TypeAdapter(list[analytics_contracts.SearchAnalyticsOrgRanking]).validate_python(
        [{"tenant_id": None, "name": "Unknown org", "event_count": 2, "quota_count": 1}]
    ) == [{"tenant_id": None, "name": "Unknown org", "event_count": 2, "quota_count": 1}]
    trending: list[analytics_contracts.SearchAnalyticsTrendingQuery] = [
        {
            "query_hash": "hash",
            "query_normalized": "query",
            "kind": "search",
            "hits": 2,
            "distinct_agents": 1,
            "distinct_orgs": 1,
        }
    ]
    assert TypeAdapter(list[analytics_contracts.SearchAnalyticsTrendingQuery]).validate_python(trending) == trending


@pytest.mark.parametrize(
    ("passthrough", "contract"),
    [
        (analytics_contracts.SearchAnalyticsSummaryPassthrough, analytics_contracts.SearchAnalyticsSummary),
        (analytics_contracts.SearchAnalyticsTimeseriesPassthrough, list[analytics_contracts.SearchAnalyticsTimeBucket]),
        (analytics_contracts.SearchAnalyticsOrgsPassthrough, list[analytics_contracts.SearchAnalyticsOrgRanking]),
        (
            analytics_contracts.SearchAnalyticsTrendingPassthrough,
            list[analytics_contracts.SearchAnalyticsTrendingQuery],
        ),
        (
            analytics_contracts.SearchAnalyticsExportStatusPassthrough,
            analytics_contracts.SearchAnalyticsExportStatusPayload,
        ),
        (tool_contracts.CategoryTestPassthrough, tool_contracts.CategoryTestResponse),
    ],
)
def test_passthrough_annotations_expose_precise_pydantic_schemas(
    passthrough: TypeForm[object], contract: TypeForm[object]
) -> None:
    assert TypeAdapter[object](passthrough).json_schema() == TypeAdapter[object](contract).json_schema()


@pytest.mark.parametrize(
    "operation",
    ["create", "bulk", "mcp", "update", "delete", "assign", "unassign", "config", "category", "remove-category"],
)
async def test_tool_mutation_wire_keeps_fixed_acknowledgements_and_empty_delete(
    monkeypatch: pytest.MonkeyPatch, tool_state: ToolState, operation: str
) -> None:
    user, agent, tool, assignment = tool_state
    tool.type = "mcp"
    tool.source = "admin"
    tool.tenant_id = user.tenant_id

    async def create(**_kwargs: object) -> ToolRecord:
        return tool

    async def update(**_kwargs: object) -> ToolRecord:
        return tool

    async def absent(*_args: object, **_kwargs: object) -> None:
        return None

    async def catalog(*_args: object) -> list[ToolRecord]:
        return [tool]

    async def assignment_get(*_args: object) -> AgentToolRecord:
        return assignment

    async def remaining(*_args: object) -> list[AgentToolRecord]:
        return [assignment]

    @asynccontextmanager
    async def transaction() -> AsyncGenerator[None]:
        yield None

    monkeypatch.setattr(tools_api, "connection_ctx", transaction)
    monkeypatch.setattr(tools_api.tool_dao, "create", create)
    monkeypatch.setattr(tools_api.tool_dao, "update", update)
    monkeypatch.setattr(tools_api.tool_dao, "delete", absent)
    monkeypatch.setattr(tools_api.tool_dao, "get_by_name_and_tenant", absent)
    monkeypatch.setattr(tools_api.tool_dao, "list_by_ids", catalog)
    monkeypatch.setattr(tools_api.tool_dao, "list_by_mcp_server", catalog)
    monkeypatch.setattr(tools_api.agent_tool_dao, "get", assignment_get)
    monkeypatch.setattr(tools_api.agent_tool_dao, "update", absent)
    monkeypatch.setattr(tools_api.agent_tool_dao, "delete", absent)
    monkeypatch.setattr(tools_api.agent_tool_dao, "delete_for_tool", absent)
    monkeypatch.setattr(tools_api.agent_tool_dao, "list_for_tool", remaining)
    monkeypatch.setattr(tools_api.channel_config_dao, "get_for_agent", absent)
    monkeypatch.setattr(tools_api.channel_config_dao, "create", absent)
    app = FastAPI()
    app.include_router(tools_api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    requests: dict[str, tuple[str, str, JsonObject | list[JsonObject] | None]] = {
        "create": ("POST", "/tools", {"name": "lookup", "display_name": "Lookup"}),
        "bulk": ("PUT", "/tools/bulk", [{"tool_id": str(tool.id), "enabled": False}]),
        "mcp": ("PUT", "/tools/mcp-server", {"server_name": "test", "server_url": "https://example.com/mcp"}),
        "update": ("PUT", f"/tools/{tool.id}", {"display_name": "Updated"}),
        "delete": ("DELETE", f"/tools/{tool.id}", None),
        "assign": ("PUT", f"/tools/agents/{agent.id}", [{"tool_id": str(tool.id), "enabled": True}]),
        "unassign": ("DELETE", f"/tools/agent-tool/{assignment.id}", None),
        "config": ("PUT", f"/tools/agents/{agent.id}/tool-config/{tool.id}", {"config": {"vendor": False}}),
        "category": (
            "POST",
            f"/tools/agents/{agent.id}/category-config/{ChannelType.SLACK.value}",
            {"config": {"vendor": False}},
        ),
        "remove-category": ("DELETE", f"/tools/agents/{agent.id}/category-config/{ChannelType.SLACK.value}", None),
    }
    method, path, payload = requests[operation]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(method, path, json=payload)

    if operation == "remove-category":
        assert response.status_code == 204
        assert response.content == b""
    elif operation == "create":
        assert response.status_code == 200
        assert TypeAdapter(tool_contracts.ToolCreatedResponse).validate_json(response.content) == {
            "id": str(tool.id),
            "name": "lookup",
        }
    elif operation == "mcp":
        assert response.status_code == 200
        assert TypeAdapter(tool_contracts.MCPServerUpdatedResponse).validate_json(response.content) == {
            "ok": True,
            "updated": 1,
        }
    else:
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert TypeAdapter(tool_contracts.ToolAcknowledgement).validate_json(response.content) == {"ok": True}
