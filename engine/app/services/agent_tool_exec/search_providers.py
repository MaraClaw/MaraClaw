from __future__ import annotations

import importlib
import re
from types import ModuleType
from urllib.parse import parse_qs, unquote, urlparse


def _httpx_module() -> ModuleType:
    return importlib.import_module("httpx")


async def _search_duckduckgo(query: str, max_results: int) -> str:
    async with _httpx_module().AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10,
        )

    results = []
    blocks = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        resp.text,
        re.DOTALL,
    )
    for url, title, snippet in blocks[:max_results]:
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        if "uddg=" in url:
            parsed = parse_qs(urlparse(url).query)
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
            api_key = value.get("api_key")
            if isinstance(api_key, str) and api_key:
                return api_key
    except Exception as exc:
        del exc
    from app.config import get_settings

    return get_settings().JINA_API_KEY


async def _search_tavily(query: str, api_key: str, max_results: int) -> str:
    async with _httpx_module().AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()

    if "results" not in data:
        return f"❌ Tavily search failed: {data.get('error', str(data)[:200])}"

    results = [
        f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('content', '')[:200]}"
        for r in data["results"][:max_results]
    ]

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Tavily search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_google(query: str, api_key: str, max_results: int, language: str) -> str:
    parts = api_key.split(":", 1)
    if len(parts) != 2:
        return "❌ Google search requires API key in format 'API_KEY:SEARCH_ENGINE_ID'"

    gapi_key, cx = parts
    async with _httpx_module().AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": gapi_key, "cx": cx, "q": query, "num": max_results, "lr": f"lang_{language[:2]}"},
            timeout=10,
        )
        data = resp.json()

    results = [
        f"**{item.get('title', '')}**\n{item.get('link', '')}\n{item.get('snippet', '')}"
        for item in data.get("items", [])[:max_results]
    ]

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Google search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_bing(query: str, api_key: str, max_results: int, language: str) -> str:
    async with _httpx_module().AsyncClient() as client:
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results, "mkt": language},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=10,
        )
        data = resp.json()

    results = [
        f"**{item.get('name', '')}**\n{item.get('url', '')}\n{item.get('snippet', '')}"
        for item in data.get("webPages", {}).get("value", [])[:max_results]
    ]

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Bing search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_exa(query: str, api_key: str, max_results: int) -> str:
    async with _httpx_module().AsyncClient() as client:
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
        data = resp.json()

    if resp.status_code != 200:
        return f"❌ Exa search failed: {data.get('error', data.get('message', str(data)[:200]))}"

    results = []
    for r in data.get("results", [])[:max_results]:
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        text = (r.get("text") or "")[:300]
        results.append(f"**{title}**\n{url}\n{text}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Exa search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)
