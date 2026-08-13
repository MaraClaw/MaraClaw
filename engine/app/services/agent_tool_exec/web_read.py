from __future__ import annotations

import asyncio
import importlib
import ipaddress
import re
import socket
from types import ModuleType
from urllib.parse import urljoin, urlparse

from app.services.agent_tool_exec.registry import ToolArguments


def _httpx_module() -> ModuleType:
    return importlib.import_module("httpx")


def _search_providers_module() -> ModuleType:
    return importlib.import_module("app.services.agent_tool_exec.search_providers")


def _beautiful_soup():
    return importlib.import_module("bs4").BeautifulSoup


def _trafilatura_module() -> ModuleType:
    return importlib.import_module("trafilatura")


async def _validate_public_http_url(url: str) -> tuple[str | None, str | None]:
    url = (url or "").strip()
    if not url:
        return None, "❌ Please provide a URL"
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None, "❌ Only HTTP and HTTPS URLs are supported"
    if not parsed.hostname:
        return None, "❌ URL must include a hostname"

    hostname = parsed.hostname
    try:
        ipaddress.ip_address(hostname)
        host_is_ip = True
    except ValueError:
        host_is_ip = False

    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        return None, "❌ Localhost URLs are blocked for safety"

    try:
        if host_is_ip:
            addresses = [hostname]
        else:
            loop = asyncio.get_running_loop()
            infos = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
                ),
            )
            addresses = [info[4][0] for info in infos]
    except Exception as exc:
        return None, f"❌ Could not resolve hostname {hostname}: {str(exc)[:160]}"

    for address in set(addresses):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None, f"❌ Could not validate resolved address: {address}"
        is_proxy_test_range = (not host_is_ip) and ip in ipaddress.ip_network("198.18.0.0/15")
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
            or (ip.is_private and not is_proxy_test_range)
        ):
            return None, f"❌ Private, local, reserved, or internal network URLs are blocked ({address})"

    return url, None


def _fallback_extract_visible_text(html: str) -> str:
    beautiful_soup = _beautiful_soup()
    soup = beautiful_soup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "canvas", "header", "footer", "nav", "aside"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_page_links(html: str, base_url: str, limit: int = 30) -> list[str]:
    beautiful_soup = _beautiful_soup()
    soup = beautiful_soup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"].strip())
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))[:80] or href
        seen.add(href)
        links.append(f"- {label}: {href}")
        if len(links) >= limit:
            break
    return links


def _string_argument(arguments: ToolArguments, name: str, default: str = "") -> str:
    value = arguments.get(name)
    return value if isinstance(value, str) else default


def _integer_argument(arguments: ToolArguments, name: str, default: int) -> int:
    value = arguments.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


async def _read_webpage(arguments: ToolArguments) -> str:
    httpx = _httpx_module()
    trafilatura = _trafilatura_module()
    beautiful_soup = _beautiful_soup()
    url, validation_error = await _validate_public_http_url(_string_argument(arguments, "url"))
    if validation_error:
        return validation_error

    max_chars = min(max(_integer_argument(arguments, "max_chars", 12000), 500), 50000)
    include_links = arguments.get("include_links") is True
    max_bytes = 2_000_000
    headers = {
        "User-Agent": "MaraClawBot/1.0 (+https://maraclaw.ai) Mozilla/5.0",
        "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, text/*;q=0.8, */*;q=0.5",
    }

    try:
        async with (
            httpx.AsyncClient(follow_redirects=True, timeout=15) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                return f"❌ Page is too large to read safely ({content_length} bytes, limit {max_bytes} bytes)"

            chunks: list[bytes] = []
            total = 0
            truncated_bytes = False
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    remaining = max_bytes - sum(len(part) for part in chunks)
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    truncated_bytes = True
                    break
                chunks.append(chunk)

            status_code = resp.status_code
            final_url = str(resp.url)
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            encoding = resp.encoding or "utf-8"

        if status_code >= 400:
            return f"❌ Webpage fetch failed HTTP {status_code}: {final_url}"

        raw = b"".join(chunks)
        text = raw.decode(encoding, errors="replace").strip()
        if not text:
            return f"❌ Empty response from {final_url}"

        title = ""
        description = ""
        extracted = text
        links: list[str] = []

        if content_type in {"", "text/html", "application/xhtml+xml"} or "<html" in text[:500].lower():
            soup = beautiful_soup(text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            meta_description = soup.find("meta", attrs={"name": "description"})
            if meta_description and meta_description.get("content"):
                description = meta_description["content"].strip()

            extracted = trafilatura.extract(
                text,
                url=final_url,
                output_format="markdown",
                include_links=include_links,
                include_comments=False,
                include_tables=True,
            ) or _fallback_extract_visible_text(text)
            if include_links:
                links = _extract_page_links(text, final_url)
        elif content_type.startswith("text/") or content_type in {"application/json", "application/xml", "text/xml"}:
            title = final_url
        else:
            return f"❌ Unsupported content type: {content_type or 'unknown'}"

        extracted = extracted.strip()
        if not extracted:
            return f"❌ Could not extract readable content from {final_url}"

        truncated_chars = len(extracted) > max_chars
        if truncated_chars:
            extracted = extracted[:max_chars].rstrip() + f"\n\n[... truncated at {max_chars} chars]"

        meta_lines = [
            f"URL: {final_url}",
            f"Status: HTTP {status_code}",
        ]
        if title:
            meta_lines.append(f"Title: {title}")
        if description:
            meta_lines.append(f"Description: {description}")
        if truncated_bytes:
            meta_lines.append(f"Note: response body truncated at {max_bytes} bytes before extraction")
        if truncated_chars:
            meta_lines.append(f"Note: extracted text truncated at {max_chars} characters")

        result = "🌐 **Webpage content**\n\n" + "\n".join(meta_lines) + "\n\n---\n\n" + extracted
        if links:
            result += "\n\n---\n\nLinks:\n" + "\n".join(links)
        return result

    except httpx.TimeoutException:
        return f"❌ Webpage fetch timed out: {url}"
    except Exception as e:
        return f"❌ Webpage read error: {str(e)[:300]}"


async def _jina_read(arguments: ToolArguments) -> str:
    httpx = _httpx_module()
    url = _string_argument(arguments, "url").strip()
    if not url:
        return "❌ Please provide a URL"
    if not url.startswith("http"):
        url = "https://" + url

    max_chars = min(_integer_argument(arguments, "max_chars", 8000), 20000)
    api_key = await _search_providers_module()._get_jina_api_key()

    headers: dict[str, str] = {
        "Accept": "text/plain, text/markdown, */*",
        "X-Return-Format": "markdown",
        "X-Remove-Selector": "header, footer, nav, aside, .ads, .advertisement",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers=headers,
            )

        if resp.status_code != 200:
            return f"❌ Jina Reader error HTTP {resp.status_code}: {resp.text[:200]}"

        text = resp.text.strip()
        if not text or len(text) < 100:
            return f"❌ Jina Reader returned empty content for {url}"

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"

        return f"📄 **Content from: {url}**\n\n{text}"

    except Exception as e:
        return f"❌ Jina Reader error: {str(e)[:300]}"
