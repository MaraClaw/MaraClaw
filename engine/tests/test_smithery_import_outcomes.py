from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.services import resource_discovery, smithery_import


@pytest.mark.parametrize(
    ("condition", "code"),
    [
        ("missing_key", "missing_credentials"),
        ("local", "unsupported_remote_transport"),
        ("registry_failure", "import_failed"),
        ("connection_failure", "connection_failed"),
        ("oauth", "authorization_required"),
        ("unauthorized", "authorization_required"),
        ("unverified", "tools_unverified"),
        ("malformed", "tools_unverified"),
        ("ready", None),
    ],
)
async def test_structured_outcome_classifies_wire_results_without_parsing_text(monkeypatch, condition, code):
    monkeypatch.setattr(smithery_import, "_saved_config", AsyncMock(return_value={}))
    monkeypatch.setattr(
        resource_discovery,
        "_get_smithery_api_key",
        AsyncMock(return_value=None if condition == "missing_key" else "secret"),
    )
    connection = {"namespace": "qa", "connection_id": "qa"}
    if condition == "connection_failure":
        connection = {"error": "private error https://oauth.invalid/?secret=credential"}
    if condition == "oauth":
        connection["auth_url"] = "https://oauth.invalid/?secret=credential"
    monkeypatch.setattr(resource_discovery, "_ensure_smithery_connection", AsyncMock(return_value=connection))

    async def wire(_transport, request):
        if request.method == "POST":
            if condition == "unauthorized":
                return httpx.Response(401, text="private error")
            if condition == "malformed":
                return httpx.Response(200, text="not json")
            tools = [] if condition == "unverified" else [{"name": "ping", "inputSchema": {}}]
            return httpx.Response(200, json={"result": {"tools": tools}})
        if request.url.path.endswith("/servers"):
            if condition == "registry_failure":
                return httpx.Response(503, text="private error")
            return httpx.Response(
                200, json={"servers": [{"qualifiedName": "vendor/qa", "remote": condition != "local"}]}
            )
        return httpx.Response(200, json={"deploymentUrl": "https://remote.invalid/mcp"})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", wire)
    outcome = await smithery_import.prepare_smithery_import("vendor/qa", uuid4())
    assert outcome.warning_code == code
    assert "private error" not in outcome.text
    if outcome.installation is not None:
        assert all(value not in str(outcome.installation.config) for value in ("oauth.invalid", "credential", "secret"))
    if condition == "ready":
        assert [tool["name"] for tool in outcome.tools] == ["ping"]
