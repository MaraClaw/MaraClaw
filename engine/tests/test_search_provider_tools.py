from __future__ import annotations

import importlib
import sys
import uuid
from collections import deque
from types import SimpleNamespace

from app.core.json_types import JsonObject, JsonValue
from app.services import agent_tools

type HttpCall = tuple[str, str, JsonObject]


def _search_providers_module():
    try:
        return importlib.import_module("app.services.agent_tool_exec.search_providers")
    except ModuleNotFoundError as exc:
        if exc.name == "app.services.agent_tool_exec.search_providers":
            return agent_tools
        raise


def _web_search_module():
    try:
        return importlib.import_module("app.services.agent_tool_exec.web_search")
    except ModuleNotFoundError as exc:
        if exc.name == "app.services.agent_tool_exec.web_search":
            return agent_tools
        raise


class _HttpResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", payload: JsonObject | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self) -> JsonObject:
        return self._payload


class _QueuedAsyncClient:
    def __init__(self, responses: deque[_HttpResponse], calls: list[HttpCall], client_kwargs: JsonObject) -> None:
        self._responses = responses
        self._calls = calls
        self._client_kwargs = client_kwargs

    async def __aenter__(self) -> _QueuedAsyncClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, url: str, **kwargs: JsonValue) -> _HttpResponse:
        self._calls.append(("GET", url, {"client_kwargs": self._client_kwargs, **kwargs}))
        return self._responses.popleft()

    async def post(self, url: str, **kwargs: JsonValue) -> _HttpResponse:
        self._calls.append(("POST", url, {"client_kwargs": self._client_kwargs, **kwargs}))
        return self._responses.popleft()


class _FakeHttpxModule:
    def __init__(self, *responses: _HttpResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[HttpCall] = []
        self.AsyncClient = self._make_client

    def _make_client(self, *args: JsonValue, **kwargs: JsonValue) -> _QueuedAsyncClient:
        del args
        return _QueuedAsyncClient(self.responses, self.calls, kwargs)


async def test_search_duckduckgo_parses_html_and_decodes_redirect(monkeypatch) -> None:
    providers = _search_providers_module()
    html = (
        '<a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.test%2Falpha">'
        "<b>Alpha</b> title</a>"
        '<a class="result__snippet">Snippet <b>one</b></a>'
        '<a class="result__a" href="https://beta.test"><span>Beta</span></a>'
        '<a class="result__snippet">Snippet two</a>'
    )
    httpx = _FakeHttpxModule(_HttpResponse(text=html))
    monkeypatch.setitem(sys.modules, "httpx", httpx)

    result = await providers._search_duckduckgo("agent tools", 1)

    assert result == (
        '🔍 DuckDuckGo results for "agent tools" (1 items):\n\n**Alpha title**\nhttps://example.test/alpha\nSnippet one'
    )
    assert httpx.calls == [
        (
            "GET",
            "https://html.duckduckgo.com/html/",
            {
                "client_kwargs": {"follow_redirects": True},
                "params": {"q": "agent tools"},
                "headers": {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                "timeout": 10,
            },
        )
    ]


async def test_search_tavily_formats_results_and_failure_without_network(monkeypatch) -> None:
    providers = _search_providers_module()
    httpx = _FakeHttpxModule(
        _HttpResponse(
            payload={"results": [{"title": "Tavily title", "url": "https://tavily.test", "content": "Tavily body"}]}
        ),
        _HttpResponse(payload={"error": "bad key"}),
    )
    monkeypatch.setitem(sys.modules, "httpx", httpx)

    success = await providers._search_tavily("platform", "tv-key", 3)
    failure = await providers._search_tavily("platform", "tv-key", 3)

    assert success == '🔍 Tavily search for "platform" (1 items):\n\n**Tavily title**\nhttps://tavily.test\nTavily body'
    assert failure == "❌ Tavily search failed: bad key"
    assert httpx.calls[0] == (
        "POST",
        "https://api.tavily.com/search",
        {
            "client_kwargs": {},
            "json": {"query": "platform", "max_results": 3, "search_depth": "basic"},
            "headers": {"Authorization": "Bearer tv-key", "Content-Type": "application/json"},
            "timeout": 15,
        },
    )


async def test_search_google_and_bing_preserve_payloads(monkeypatch) -> None:
    providers = _search_providers_module()
    httpx = _FakeHttpxModule(
        _HttpResponse(
            payload={"items": [{"title": "Google title", "link": "https://google.test", "snippet": "Google body"}]}
        ),
        _HttpResponse(
            payload={
                "webPages": {"value": [{"name": "Bing title", "url": "https://bing.test", "snippet": "Bing body"}]}
            }
        ),
    )
    monkeypatch.setitem(sys.modules, "httpx", httpx)

    invalid_google = await providers._search_google("query", "missing-cx", 2, "en")
    google = await providers._search_google("query", "g-key:cx-id", 2, "zh-CN")
    bing = await providers._search_bing("query", "bing-key", 2, "en-US")

    assert invalid_google == "❌ Google search requires API key in format 'API_KEY:SEARCH_ENGINE_ID'"
    assert google == '🔍 Google search for "query" (1 items):\n\n**Google title**\nhttps://google.test\nGoogle body'
    assert bing == '🔍 Bing search for "query" (1 items):\n\n**Bing title**\nhttps://bing.test\nBing body'
    assert httpx.calls == [
        (
            "GET",
            "https://www.googleapis.com/customsearch/v1",
            {
                "client_kwargs": {},
                "params": {"key": "g-key", "cx": "cx-id", "q": "query", "num": 2, "lr": "lang_zh"},
                "timeout": 10,
            },
        ),
        (
            "GET",
            "https://api.bing.microsoft.com/v7.0/search",
            {
                "client_kwargs": {},
                "params": {"q": "query", "count": 2, "mkt": "en-US"},
                "headers": {"Ocp-Apim-Subscription-Key": "bing-key"},
                "timeout": 10,
            },
        ),
    ]


async def test_search_exa_formats_success_and_http_error(monkeypatch) -> None:
    providers = _search_providers_module()
    httpx = _FakeHttpxModule(
        _HttpResponse(payload={"results": [{"title": "Exa title", "url": "https://exa.test", "text": "Exa body"}]}),
        _HttpResponse(status_code=401, payload={"message": "unauthorized"}),
    )
    monkeypatch.setitem(sys.modules, "httpx", httpx)

    success = await providers._search_exa("semantic", "exa-key", 4)
    failure = await providers._search_exa("semantic", "exa-key", 4)

    assert success == '🔍 Exa search for "semantic" (1 items):\n\n**Exa title**\nhttps://exa.test\nExa body'
    assert failure == "❌ Exa search failed: unauthorized"
    assert httpx.calls[0] == (
        "POST",
        "https://api.exa.ai/search",
        {
            "client_kwargs": {},
            "json": {
                "query": "semantic",
                "type": "auto",
                "numResults": 4,
                "contents": {"text": {"maxCharacters": 1000}},
            },
            "headers": {"x-api-key": "exa-key", "Content-Type": "application/json", "x-exa-integration": "maraclaw"},
            "timeout": 15,
        },
    )


async def test_get_jina_api_key_prefers_db_and_falls_back_to_env(monkeypatch) -> None:
    providers = _search_providers_module()

    class Result:
        def __init__(self, setting) -> None:
            self._setting = setting

        def scalar_one_or_none(self):
            return self._setting

    class DB:
        def __init__(self, setting) -> None:
            self._setting = setting

        async def execute(self, _statement) -> Result:
            return Result(self._setting)

    class Session:
        def __init__(self, setting) -> None:
            self._setting = setting

        async def __aenter__(self) -> DB:
            return DB(self._setting)

        async def __aexit__(self, *_args) -> bool:
            return False

    from unittest.mock import AsyncMock

    from app.dao.system_setting_dao import system_setting_dao

    config = importlib.import_module("app.config")
    monkeypatch.setattr(
        system_setting_dao,
        "get_value",
        AsyncMock(return_value={"api_key": "db-jina"}),
    )

    assert await providers._get_jina_api_key() == "db-jina"

    async def failing_get_value(*_a, **_k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(system_setting_dao, "get_value", failing_get_value)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(JINA_API_KEY="env-jina"))

    assert await providers._get_jina_api_key() == "env-jina"


async def test_jina_search_formats_results_and_uses_optional_api_key(monkeypatch) -> None:
    web_search = _web_search_module()
    providers = _search_providers_module()
    httpx = _FakeHttpxModule(
        _HttpResponse(
            payload={
                "data": [
                    {"title": "Jina title", "url": "https://jina.test", "description": "Jina description"},
                    {"title": "Second", "url": "https://second.test", "content": "Second content"},
                ]
            }
        )
    )

    async def get_jina_api_key() -> str:
        return "jina-key"

    monkeypatch.setitem(sys.modules, "httpx", httpx)
    monkeypatch.setattr(providers, "_get_jina_api_key", get_jina_api_key, raising=False)
    monkeypatch.setattr(web_search, "_get_jina_api_key", get_jina_api_key, raising=False)

    result = await web_search._jina_search({"query": "agent tools", "max_results": 1})

    assert result == (
        '🔍 Jina Search results for "agent tools" (1 items):\n\n**1. Jina title**\nhttps://jina.test\nJina description'
    )
    assert httpx.calls == [
        (
            "GET",
            "https://s.jina.ai/agent%20tools",
            {
                "client_kwargs": {"follow_redirects": True, "timeout": 30},
                "headers": {
                    "Accept": "application/json",
                    "X-Respond-With": "no-content",
                    "X-Return-Format": "markdown",
                    "Authorization": "Bearer jina-key",
                },
            },
        )
    ]


async def test_web_search_selector_uses_configured_provider_and_facade_config(monkeypatch) -> None:
    web_search = _web_search_module()
    providers = _search_providers_module()
    agent_id = uuid.uuid4()
    calls = []

    async def get_tool_config(observed_agent_id: uuid.UUID, tool_name: str) -> JsonObject:
        calls.append(("config", observed_agent_id, tool_name))
        return {"search_engine": "tavily", "api_key": "tv-key", "max_results": 7, "language": "ja"}

    async def search_tavily(query: str, api_key: str, max_results: int) -> str:
        calls.append(("tavily", query, api_key, max_results))
        return "tavily result"

    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    monkeypatch.setattr(providers, "_search_tavily", search_tavily, raising=False)
    monkeypatch.setattr(web_search, "_search_tavily", search_tavily, raising=False)

    result = await web_search._web_search({"query": "platform", "max_results": 9}, agent_id)

    assert result == "tavily result"
    assert calls == [
        ("config", agent_id, "web_search"),
        ("tavily", "platform", "tv-key", 9),
    ]


async def test_standalone_search_wrapper_messages_and_config_seams(monkeypatch) -> None:
    web_search = _web_search_module()
    providers = _search_providers_module()
    calls = []

    async def get_tool_config(_agent_id: uuid.UUID | None, tool_name: str) -> JsonObject:
        calls.append(("config", tool_name))
        return {}

    async def search_duckduckgo(query: str, max_results: int) -> str:
        calls.append(("duck", query, max_results))
        return "duck result"

    monkeypatch.setattr(agent_tools, "_get_tool_config", get_tool_config)
    monkeypatch.setattr(providers, "_search_duckduckgo", search_duckduckgo, raising=False)
    monkeypatch.setattr(web_search, "_search_duckduckgo", search_duckduckgo, raising=False)

    assert await web_search._duckduckgo_search_tool({}) == "Please provide search keywords"
    assert await web_search._duckduckgo_search_tool({"query": "duck", "max_results": 12}) == "duck result"
    assert await web_search._tavily_search_tool({"query": "tv"}, uuid.uuid4()) == (
        "Tavily API key is required. Set it in the tool settings."
    )
    assert await web_search._google_search_tool({"query": "g"}, uuid.uuid4()) == (
        "Google Search API key is required (format: API_KEY:SEARCH_ENGINE_ID). Set it in the tool settings."
    )
    assert await web_search._bing_search_tool({"query": "b"}, uuid.uuid4()) == (
        "Bing Search API key is required. Set it in the tool settings."
    )
    assert calls == [
        ("duck", "duck", 10),
        ("config", "tavily_search"),
        ("config", "google_search"),
        ("config", "bing_search"),
    ]
