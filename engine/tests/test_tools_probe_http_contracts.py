from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import atlassian, tools
from app.core import outbound_policy
from app.core.domain.users import UserRole
from app.core.json_types import JsonObject
from app.records.user import UserRecord
from app.services import agentbay_client, mcp_client


@pytest.mark.parametrize("success", [True, False])
async def test_mcp_probe_http_preserves_success_and_failure_payloads(
    monkeypatch: pytest.MonkeyPatch,
    success: bool,
) -> None:
    # Given an isolated MCP boundary, without DNS resolution or provider requests.
    payload: list[JsonObject] = [{"name": "search", "inputSchema": {"vendor": [None, False]}, "extension": None}]

    async def validate(url: str) -> str:
        return url

    async def list_tools(self: mcp_client.MCPClient) -> list[JsonObject]:
        if not success:
            raise RuntimeError("provider unavailable")
        return payload

    monkeypatch.setattr(outbound_policy, "validate_public_url", validate)
    monkeypatch.setattr(mcp_client.MCPClient, "list_tools", list_tools)
    app = FastAPI()
    app.include_router(tools.router, prefix="/api")
    app.dependency_overrides[tools.get_current_user] = lambda: UserRecord(id=uuid4(), role=UserRole.MEMBER)
    # When testing connectivity through the actual HTTP response serializer.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/tools/test-mcp", json={"server_url": "https://provider.invalid/mcp"})
    app.dependency_overrides.clear()
    # Then the existing tagged success/failure shape and open provider metadata survive.
    expected: object = {"ok": True, "tools": payload} if success else {"ok": False, "error": "provider unavailable"}
    assert (response.status_code, response.json()) == (200, expected)


@pytest.mark.parametrize(
    ("category", "payload"),
    [
        (
            "atlassian",
            {"ok": True, "message": "connected", "tool_count": 1, "tools": [{"name": None, "description": "preview"}]},
        ),
        ("atlassian", {"ok": False, "error": "provider unavailable"}),
        ("agentbay", {"ok": True, "message": "connected"}),
        ("agentbay", {"ok": False, "error": "provider unavailable"}),
    ],
)
async def test_category_probe_http_preserves_provider_variants(
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    payload: JsonObject,
) -> None:
    # Given provider-category boundaries returning the existing success/failure variants.
    async def probe(agent_id: UUID, current_user: UserRecord) -> JsonObject:
        return payload

    monkeypatch.setattr(atlassian, "check_atlassian_channel", probe)
    monkeypatch.setattr(agentbay_client, "check_agentbay_channel", probe)
    app = FastAPI()
    app.include_router(tools.router, prefix="/api")
    app.dependency_overrides[tools.get_current_user] = lambda: UserRecord(id=uuid4(), role=UserRole.MEMBER)
    # When testing the category using its real route.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/tools/agents/{uuid4()}/category-config/{category}/test")
    app.dependency_overrides.clear()
    # Then union validation retains all variant-specific fields, including nullable preview names.
    assert (response.status_code, response.json()) == (200, payload)
