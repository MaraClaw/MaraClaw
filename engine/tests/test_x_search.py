import sys
import uuid
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.agent_tool_exec.x_search as x_search


def clear_xai_env(monkeypatch):
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(XAI_API_KEY=""))


class FakeResponse:
    def __init__(self, status_code, payload=None, *, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, transport, client_kwargs):
        self._transport = transport
        self._client_kwargs = client_kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def post(self, url, **kwargs):
        return self._transport.request("POST", url, self._client_kwargs, kwargs)


class FakeTimeoutError(Exception):
    pass


class FakeHTTPX:
    def __init__(self, responses):
        self._responses = deque(responses)
        self.calls = []
        self.__dict__["TimeoutException"] = FakeTimeoutError
        self.__dict__["AsyncClient"] = self._new_async_client

    def _new_async_client(self, **kwargs):
        return FakeAsyncClient(self, kwargs)

    def request(self, method, url, client_kwargs, kwargs):
        self.calls.append((method, url, client_kwargs, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected {method} request to {url}")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def install_fake_httpx(monkeypatch, responses):
    fake_httpx = FakeHTTPX(responses)
    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    monkeypatch.setattr(x_search, "httpx", fake_httpx)
    return fake_httpx


@pytest.mark.asyncio
async def test_search_x_requires_query(monkeypatch):
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"api_key": "key"}))
    assert await x_search._search_x(uuid.uuid4(), {}) == "❌ Missing required argument 'query' for search_x"


@pytest.mark.asyncio
async def test_search_x_requires_api_key(monkeypatch):
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={}))
    result = await x_search._search_x(uuid.uuid4(), {"query": "xAI"})
    assert result.startswith("❌ X search API key not configured")


@pytest.mark.asyncio
async def test_search_x_rejects_allowed_and_excluded_together(monkeypatch):
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"api_key": "key"}))
    result = await x_search._search_x(
        uuid.uuid4(),
        {"query": "xAI", "allowed_x_handles": "xai", "excluded_x_handles": "openai"},
    )
    assert result == "❌ Use either allowed_x_handles or excluded_x_handles, not both"


@pytest.mark.asyncio
async def test_search_x_posts_responses_payload_and_formats_citations(monkeypatch):
    fake_httpx = install_fake_httpx(
        monkeypatch,
        [
            FakeResponse(
                200,
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "People are talking about grok-4.6."}],
                        }
                    ],
                    "citations": ["https://x.com/xai/status/1", "https://x.com/elonmusk/status/2"],
                },
            )
        ],
    )
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    config = AsyncMock(return_value={"api_key": "xai-key", "model": "grok-4.6", "base_url": "https://api.x.ai/v1/"})
    monkeypatch.setattr(agent_tools, "_get_tool_config", config)
    agent_id = uuid.uuid4()

    result = await x_search._search_x(
        agent_id,
        {
            "query": "What are people saying about xAI?",
            "allowed_x_handles": "@xai, elonmusk",
            "from_date": "2026-08-01",
            "enable_image_understanding": True,
        },
    )

    assert result == (
        "People are talking about grok-4.6.\n\n"
        "Citations:\n- https://x.com/xai/status/1\n- https://x.com/elonmusk/status/2"
    )
    config.assert_awaited_once_with(agent_id, "search_x")
    assert len(fake_httpx.calls) == 1
    method, url, client_kwargs, kwargs = fake_httpx.calls[0]
    assert method == "POST"
    assert url == "https://api.x.ai/v1/responses"
    assert client_kwargs == {"timeout": 120}
    assert kwargs["headers"] == {"Authorization": "Bearer xai-key", "Content-Type": "application/json"}
    payload = kwargs["json"]
    assert payload["model"] == "grok-4.6"
    assert payload["tools"] == [
        {
            "type": "x_search",
            "allowed_x_handles": ["xai", "elonmusk"],
            "from_date": "2026-08-01",
            "enable_image_understanding": True,
        }
    ]
    assert "<query>" in payload["input"][0]["content"]
    assert "What are people saying about xAI?" in payload["input"][0]["content"]


@pytest.mark.asyncio
async def test_search_x_preserves_provider_error(monkeypatch):
    install_fake_httpx(monkeypatch, [FakeResponse(429, {"message": "rate limited"}, text="fallback")])
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"api_key": "key"}))

    result = await x_search._search_x(uuid.uuid4(), {"query": "xAI"})

    assert result == "❌ X search failed (429): rate limited"


@pytest.mark.asyncio
async def test_search_x_reports_timeout(monkeypatch):
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    install_fake_httpx(monkeypatch, [FakeTimeoutError("slow")])
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"api_key": "key"}))

    result = await x_search._search_x(uuid.uuid4(), {"query": "xAI"})

    assert result.startswith("❌ X search timed out after 120 seconds")


@pytest.mark.asyncio
async def test_search_x_uses_env_key_and_default_model(monkeypatch):
    from app.services import agent_tools

    fake_httpx = install_fake_httpx(monkeypatch, [FakeResponse(200, {"output_text": "ok"})])
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(XAI_API_KEY="env-key"))
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={}))

    result = await x_search._search_x(uuid.uuid4(), {"query": "xAI", "excluded_x_handles": ["openai"]})

    assert result == "ok"
    payload = fake_httpx.calls[0][3]["json"]
    assert payload["model"] == "grok-4.6"
    assert payload["tools"] == [{"type": "x_search", "excluded_x_handles": ["openai"]}]
    assert fake_httpx.calls[0][3]["headers"]["Authorization"] == "Bearer env-key"


@pytest.mark.asyncio
async def test_search_x_rejects_untrusted_base_url(monkeypatch):
    from app.services import agent_tools

    fake_httpx = install_fake_httpx(monkeypatch, [])
    clear_xai_env(monkeypatch)
    monkeypatch.setattr(
        agent_tools,
        "_get_tool_config",
        AsyncMock(return_value={"api_key": "key", "base_url": "https://evil.test/v1"}),
    )

    result = await x_search._search_x(uuid.uuid4(), {"query": "xAI"})

    assert result == "❌ xAI base_url must be https://api.x.ai/v1"
    assert fake_httpx.calls == []


@pytest.mark.asyncio
async def test_search_x_rejects_invalid_handles_dates_and_empty_output(monkeypatch):
    from app.services import agent_tools

    clear_xai_env(monkeypatch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"api_key": "key"}))

    assert (
        await x_search._search_x(uuid.uuid4(), {"query": "xAI", "allowed_x_handles": "bad handle"})
        == "❌ X handles must be 1-30 letters, digits, or underscores (no @)"
    )
    assert (
        await x_search._search_x(uuid.uuid4(), {"query": "xAI", "from_date": "08-01-2026"})
        == "❌ from_date must be YYYY-MM-DD"
    )
    assert (
        await x_search._search_x(uuid.uuid4(), {"query": "x" * 2001})
        == "❌ query must be at most 2000 characters"
    )

    install_fake_httpx(monkeypatch, [FakeResponse(200, {"output": []})])
    assert await x_search._search_x(uuid.uuid4(), {"query": "xAI"}) == (
        "❌ X search returned no text. Try a more specific query."
    )


@pytest.mark.asyncio
async def test_search_x_sends_video_understanding_flag(monkeypatch):
    from app.services import agent_tools

    fake_httpx = install_fake_httpx(monkeypatch, [FakeResponse(200, {"output_text": "video"})])
    clear_xai_env(monkeypatch)
    monkeypatch.setattr(agent_tools, "_get_tool_config", AsyncMock(return_value={"api_key": "key"}))

    result = await x_search._search_x(uuid.uuid4(), {"query": "xAI", "enable_video_understanding": True})

    assert result == "video"
    assert fake_httpx.calls[0][3]["json"]["tools"] == [
        {"type": "x_search", "enable_video_understanding": True}
    ]
