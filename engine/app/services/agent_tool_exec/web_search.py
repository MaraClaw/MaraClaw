from __future__ import annotations

import uuid
from urllib.parse import quote

from httpx import AsyncClient, Response

from app.config import get_settings
from app.core.json_types import JsonObject, json_as_str, json_as_str_or, json_object_from_response
from app.services import agent_tools
from app.services.agent_tool_exec.registry import ToolArguments, ToolArgumentValue

from . import search_providers


def _search_providers_module():
    return search_providers


def _httpx_client(*, timeout: float = 5.0, follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


def _response_mapping(response: Response) -> JsonObject:
    return json_object_from_response(response)


def _object_items(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _web_search(arguments: ToolArguments, agent_id: uuid.UUID | None = None) -> str:
    query = _string_argument(arguments, "query")
    if not query:
        return "❌ Please provide search keywords"

    config = await agent_tools._get_tool_config(agent_id, "web_search") or {}

    engine = json_as_str_or(config.get("search_engine"), "duckduckgo")
    api_key = json_as_str_or(config.get("api_key"))
    configured_max_results = config.get("max_results", 5)
    max_results = min(
        _integer_argument(
            arguments, "max_results", configured_max_results if isinstance(configured_max_results, int) else 5
        ),
        10,
    )
    language = json_as_str_or(config.get("language"), "zh-CN")

    try:
        if engine == "tavily" and api_key:
            return await _search_providers_module()._search_tavily(query, api_key, max_results)
        if engine == "google" and api_key:
            return await _search_providers_module()._search_google(query, api_key, max_results, language)
        if engine == "bing" and api_key:
            return await _search_providers_module()._search_bing(query, api_key, max_results, language)
        if engine == "exa" and api_key:
            return await _search_providers_module()._search_exa(query, api_key, max_results)
        return await _search_providers_module()._search_duckduckgo(query, max_results)
    except Exception as e:
        return f"❌ Search error ({engine}): {str(e)[:200]}"


async def _jina_search(arguments: ToolArguments) -> str:
    query = _string_argument(arguments, "query").strip()
    if not query:
        return "❌ Please provide search keywords"

    max_results = min(_integer_argument(arguments, "max_results", 5), 10)
    api_key = await _search_providers_module()._get_jina_api_key()

    headers: dict[str, str] = {
        "Accept": "application/json",
        "X-Respond-With": "no-content",
        "X-Return-Format": "markdown",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with _httpx_client(follow_redirects=True, timeout=30) as client:
            resp = await client.get(
                f"https://s.jina.ai/{quote(query)}",
                headers=headers,
            )

        if resp.status_code != 200:
            return f"❌ Jina Search error HTTP {resp.status_code}: {resp.text[:200]}"

        data = _response_mapping(resp)
        items = _object_items(data.get("data"))[:max_results]

        if not items:
            return f'🔍 No results found for "{query}"'

        parts = []
        for i, item in enumerate(items, 1):
            title = json_as_str(item.get("title")) or "Untitled"
            url = json_as_str(item.get("url")) or ""
            description = json_as_str(item.get("description")) or (json_as_str(item.get("content")) or "")[:500]
            parts.append(f"**{i}. {title}**\n{url}\n{description}")

        return f'🔍 Jina Search results for "{query}" ({len(items)} items):\n\n' + "\n\n---\n\n".join(parts)

    except Exception as e:
        return f"❌ Jina Search error: {str(e)[:300]}"


async def _exa_search(arguments: ToolArguments, agent_id: uuid.UUID | None = None) -> str:
    query = _string_argument(arguments, "query").strip()
    if not query:
        return "❌ Please provide search keywords"

    config = await agent_tools._get_tool_config(agent_id, "exa_search") or {}
    api_key = json_as_str_or(config.get("api_key")) or get_settings().EXA_API_KEY
    if not api_key:
        return "❌ Exa API key is required. Set it in tool settings or the EXA_API_KEY environment variable."

    max_results = min(_integer_argument(arguments, "max_results", 5), 10)
    search_type = _string_argument(arguments, "search_type", "auto")
    category = _string_argument(arguments, "category") or None
    content_mode = _string_argument(arguments, "content_mode", "text")
    include_domains = _string_argument(arguments, "include_domains")
    exclude_domains = _string_argument(arguments, "exclude_domains")

    contents: dict[str, ToolArgumentValue] = {}
    body: dict[str, ToolArgumentValue] = {
        "query": query,
        "type": search_type,
        "numResults": max_results,
        "contents": contents,
    }

    if category:
        body["category"] = category
    if include_domains:
        body["includeDomains"] = [d.strip() for d in include_domains.split(",") if d.strip()]
    if exclude_domains:
        body["excludeDomains"] = [d.strip() for d in exclude_domains.split(",") if d.strip()]

    if content_mode == "highlights":
        contents["highlights"] = {"numSentences": 3}
    elif content_mode == "summary":
        contents["summary"] = {}
    else:
        contents["text"] = {"maxCharacters": 1000}

    try:
        async with _httpx_client() as client:
            resp = await client.post(
                "https://api.exa.ai/search",
                json=body,
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                    "x-exa-integration": "maraclaw",
                },
                timeout=15,
            )
            data = _response_mapping(resp)

        if resp.status_code != 200:
            return f"❌ Exa search failed: {json_as_str(data.get('error')) or json_as_str(data.get('message')) or str(data)[:200]}"

        items = _object_items(data.get("results"))[:max_results]
        if not items:
            return f'🔍 No results found for "{query}"'

        parts = []
        for i, r in enumerate(items, 1):
            title = json_as_str(r.get("title")) or "Untitled"
            url = json_as_str(r.get("url")) or ""
            content = ""
            highlights = r.get("highlights")
            summary = r.get("summary")
            text = json_as_str(r.get("text"))
            if content_mode == "highlights" and isinstance(highlights, list) and highlights:
                content = " ... ".join(item for item in highlights if isinstance(item, str))
            elif content_mode == "summary" and summary:
                content = json_as_str(summary) or ""
            elif text:
                content = text[:500]
            parts.append(f"**{i}. {title}**\n{url}\n{content}")

        return f'🔍 Exa search for "{query}" ({len(items)} items):\n\n' + "\n\n---\n\n".join(parts)

    except Exception as e:
        return f"❌ Exa search error: {str(e)[:300]}"


async def _duckduckgo_search_tool(arguments: ToolArguments) -> str:
    query = _string_argument(arguments, "query").strip()
    if not query:
        return "Please provide search keywords"
    max_results = min(_integer_argument(arguments, "max_results", 5), 10)
    return await _search_providers_module()._search_duckduckgo(query, max_results)


async def _tavily_search_tool(arguments: ToolArguments, agent_id: uuid.UUID | None = None) -> str:
    query = _string_argument(arguments, "query").strip()
    if not query:
        return "Please provide search keywords"
    config = await agent_tools._get_tool_config(agent_id, "tavily_search") or {}
    api_key = json_as_str_or(config.get("api_key")).strip()
    if not api_key:
        return "Tavily API key is required. Set it in the tool settings."
    max_results = min(_integer_argument(arguments, "max_results", 5), 10)
    try:
        return await _search_providers_module()._search_tavily(query, api_key, max_results)
    except Exception as e:
        return f"Tavily search error: {str(e)[:200]}"


async def _google_search_tool(arguments: ToolArguments, agent_id: uuid.UUID | None = None) -> str:
    query = _string_argument(arguments, "query").strip()
    if not query:
        return "Please provide search keywords"
    config = await agent_tools._get_tool_config(agent_id, "google_search") or {}
    api_key = json_as_str_or(config.get("api_key")).strip()
    if not api_key:
        return "Google Search API key is required (format: API_KEY:SEARCH_ENGINE_ID). Set it in the tool settings."
    language = _string_argument(arguments, "language") or json_as_str_or(config.get("language"), "en")
    max_results = min(_integer_argument(arguments, "max_results", 5), 10)
    try:
        return await _search_providers_module()._search_google(query, api_key, max_results, language)
    except Exception as e:
        return f"Google search error: {str(e)[:200]}"


async def _bing_search_tool(arguments: ToolArguments, agent_id: uuid.UUID | None = None) -> str:
    query = _string_argument(arguments, "query").strip()
    if not query:
        return "Please provide search keywords"
    config = await agent_tools._get_tool_config(agent_id, "bing_search") or {}
    api_key = json_as_str_or(config.get("api_key")).strip()
    if not api_key:
        return "Bing Search API key is required. Set it in the tool settings."
    language = _string_argument(arguments, "language") or json_as_str_or(config.get("language"), "en-US")
    max_results = min(_integer_argument(arguments, "max_results", 5), 10)
    try:
        return await _search_providers_module()._search_bing(query, api_key, max_results, language)
    except Exception as e:
        return f"Bing search error: {str(e)[:200]}"
