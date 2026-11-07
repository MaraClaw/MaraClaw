from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from tests.test_tavily_proxy_client import FakeResponse, MemoryKeyDao, SharedWire, limits, recorded, ring, shared_wire

from app.api import tavily_proxy
from app.dao.tavily_proxy_dao import TavilyAgentAvailability
from app.services.tavily import tokens
from app.services.tavily.keys import add_key

__all__ = ["limits", "recorded", "ring", "shared_wire"]


@pytest.mark.asyncio
async def test_authenticated_search_and_crawl_rotation_at_http_boundary(
    ring: MemoryKeyDao, shared_wire: SharedWire, limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id = uuid4()
    token = tokens._proxy_token(agent_id, secret="test-secret")
    monkeypatch.setattr(tokens, "get_settings", lambda: limits)
    monkeypatch.setattr(tavily_proxy, "get_settings", lambda: limits)
    availability = AsyncMock(return_value=TavilyAgentAvailability(tenant_id=uuid4()))
    monkeypatch.setattr(tavily_proxy, "agent_available", availability)
    await add_key(label="first", api_key="provider-first")
    await add_key(label="second", api_key="provider-second")
    shared_wire.responses = [
        FakeResponse(200, '{"results":[{"url":"https://example.com","content":"found"}]}'),
        FakeResponse(433, '{"detail":{"error":"plan limit"}}'),
        FakeResponse(
            200,
            '{"base_url":"https://example.com","results":[{"url":"https://example.com","raw_content":"page"}],'
            '"response_time":1.5,"request_id":"crawl-id"}',
        ),
    ]
    app = FastAPI()
    app.include_router(tavily_proxy.router)
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test") as http:
        denied = await http.post("/api/tavily/v1/search", headers={"Authorization": "Bearer invalid"}, json={})
        search = await http.post(
            "/api/tavily/v1/search", headers={"Authorization": f"Bearer {token}"}, json={"query": "example"}
        )
        crawl = await http.post(
            "/api/tavily/v1/crawl",
            headers={"Authorization": f"Bearer {token}"},
            json={"url": "https://example.com", "extract_depth": "advanced", "format": "text", "timeout": 30},
        )

    assert denied.status_code == 401
    assert search.status_code == 200
    assert search.json()["results"][0]["content"] == "found"
    assert crawl.status_code == 200
    assert crawl.json() == {
        "base_url": "https://example.com",
        "results": [{"url": "https://example.com", "raw_content": "page"}],
        "response_time": 1.5,
        "request_id": "crawl-id",
    }
    assert [(str(r.url), r.headers["authorization"]) for r in shared_wire.requests] == [
        ("https://api.tavily.com/search", "Bearer provider-first"),
        ("https://api.tavily.com/crawl", "Bearer provider-first"),
        ("https://api.tavily.com/crawl", "Bearer provider-second"),
    ]
    assert shared_wire.requests[0].content == b'{"query":"example"}'
    assert (
        shared_wire.requests[1].content
        == shared_wire.requests[2].content
        == b'{"url":"https://example.com","extract_depth":"advanced","format":"text","timeout":30}'
    )
    availability.assert_any_await(agent_id, sha256(token.encode()).digest())
    assert token not in search.text + crawl.text
