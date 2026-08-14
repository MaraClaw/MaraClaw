from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from httpx import AsyncClient, Response

from app.core.json_types import JsonObject, json_as_str, json_object_from_response


def _httpx_client(*, timeout: float = 5.0, follow_redirects: bool = False) -> AsyncClient:
    return AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


def _response_mapping(response: Response) -> JsonObject:
    return json_object_from_response(response)


def _nested_mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _object_items(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


async def _search_duckduckgo(query: str, max_results: int) -> str:
    async with _httpx_client(follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10,
        )

    results = []
    blocks = list[object](
        re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
            + r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            resp.text,
            re.DOTALL,
        )
    )
    for block in blocks[:max_results]:
        if not isinstance(block, tuple) or len(block) != 3:
            continue
        url_raw, title_raw, snippet_raw = block
        if not isinstance(url_raw, str) or not isinstance(title_raw, str) or not isinstance(snippet_raw, str):
            continue
        url, title, snippet = url_raw, title_raw, snippet_raw
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        if "uddg=" in url:
            parsed: dict[str, list[str]] = parse_qs(urlparse(url).query)
            url = unquote(parsed.get("uddg", [url])[0])
        results.append(f"**{title}**\n{url}\n{snippet}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 DuckDuckGo results for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _get_jina_api_key() -> str:
    try:
        from app.dao.system_setting_dao import system_setting_dao

        value = await system_setting_dao.get_value("jina_api_key", {})
        if isinstance(value, dict):
            api_key = json_as_str(value.get("api_key"))
            if api_key:
                return api_key
    except Exception as exc:
        del exc
    from app.config import get_settings

    return get_settings().JINA_API_KEY


async def _search_tavily(query: str, api_key: str, max_results: int) -> str:
    async with _httpx_client() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = _response_mapping(resp)

    if "results" not in data:
        return f"❌ Tavily search failed: {json_as_str(data.get('error')) or str(data)[:200]}"

    results = [
        f"**{r.get('title', '')}**\n{r.get('url', '')}\n{str(r.get('content', '') or '')[:200]}"
        for r in _object_items(data.get("results"))[:max_results]
    ]

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Tavily search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_google(query: str, api_key: str, max_results: int, language: str) -> str:
    parts = api_key.split(":", 1)
    if len(parts) != 2:
        return "❌ Google search requires API key in format 'API_KEY:SEARCH_ENGINE_ID'"

    gapi_key, cx = parts
    async with _httpx_client() as client:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": gapi_key, "cx": cx, "q": query, "num": max_results, "lr": f"lang_{language[:2]}"},
            timeout=10,
        )
        data = _response_mapping(resp)

    results = [
        f"**{item.get('title', '')}**\n{item.get('link', '')}\n{item.get('snippet', '')}"
        for item in _object_items(data.get("items"))[:max_results]
    ]

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Google search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_bing(query: str, api_key: str, max_results: int, language: str) -> str:
    async with _httpx_client() as client:
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results, "mkt": language},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=10,
        )
        data = _response_mapping(resp)

    results = [
        f"**{item.get('name', '')}**\n{item.get('url', '')}\n{item.get('snippet', '')}"
        for item in _object_items(_nested_mapping(data.get("webPages")).get("value"))[:max_results]
    ]

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Bing search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_exa(query: str, api_key: str, max_results: int) -> str:
    async with _httpx_client() as client:
        resp = await client.post(
            "https://api.exa.ai/search",
            json={
                "query": query,
                "type": "auto",
                "numResults": max_results,
                "contents": {"text": {"maxCharacters": 1000}},
            },
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

    results = []
    for r in _object_items(data.get("results"))[:max_results]:
        title = json_as_str(r.get("title")) or "Untitled"
        url = json_as_str(r.get("url")) or ""
        text = (json_as_str(r.get("text")) or "")[:300]
        results.append(f"**{title}**\n{url}\n{text}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Exa search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)
