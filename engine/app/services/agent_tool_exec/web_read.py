from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from typing import Protocol, TypeIs, cast
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from httpx import AsyncClient, Response, TimeoutException

from app.core.outbound_policy import OutboundPolicyError, validate_public_url
from app.core.outbound_transport import public_http_client
from app.services.agent_tool_exec.registry import ToolArguments

type TrafilaturaExtract = Callable[..., object]


class _TrafilaturaModule(Protocol):
    extract: TrafilaturaExtract


def _is_trafilatura_module(value: object) -> TypeIs[_TrafilaturaModule]:
    return callable(getattr(value, "extract", None))


def _httpx_client(*, follow_redirects: bool, timeout: float) -> AsyncClient:
    return public_http_client(follow_redirects=follow_redirects, timeout=timeout)


def _beautiful_soup() -> type[BeautifulSoup]:
    return BeautifulSoup


def _trafilatura_extract() -> TrafilaturaExtract:
    module: object = importlib.import_module("trafilatura")
    if not _is_trafilatura_module(module):
        raise TypeError("trafilatura.extract is unavailable")
    return module.extract


async def _validate_public_http_url(url: str) -> tuple[str | None, str | None]:
    url = (url or "").strip()
    if not url:
        return None, "❌ Please provide a URL"
    if "://" not in url:
        url = "https://" + url

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        return None, "❌ Invalid outbound URL"
    if parsed.scheme not in {"http", "https"}:
        return None, "❌ Only HTTP and HTTPS URLs are supported"
    if hostname in (None, ""):
        return None, "❌ URL must include a hostname"

    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        return None, "❌ Localhost URLs are blocked for safety"

    try:
        return await validate_public_url(url), None
    except OutboundPolicyError as exc:
        return None, f"❌ {exc}"


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
        href_raw = anchor.get("href")
        href_text = href_raw.strip() if isinstance(href_raw, str) else ""
        if not href_text:
            continue
        href = urljoin(base_url, href_text)
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
    extract_readable = _trafilatura_extract()
    beautiful_soup = _beautiful_soup()
    url, validation_error = await _validate_public_http_url(_string_argument(arguments, "url"))
    if validation_error not in (None, "") or url is None:
        return validation_error or "❌ Please provide a URL"

    max_chars = min(max(_integer_argument(arguments, "max_chars", 12000), 500), 50000)
    include_links = arguments.get("include_links") is True
    max_bytes = 2_000_000
    headers = {
        "User-Agent": "MaraClawBot/1.0 (+https://maraclaw.ai) Mozilla/5.0",
        "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, text/*;q=0.8, */*;q=0.5",
    }

    try:
        async with _httpx_client(follow_redirects=False, timeout=15) as client:
            redirects = 0
            while True:
                async with client.stream("GET", url, headers=headers) as streamed:
                    resp: Response = streamed
                    location: str | None = None
                    if "location" in resp.headers:
                        location = resp.headers["location"]
                    if resp.status_code in {301, 302, 303, 307, 308} and location is not None:
                        if redirects >= 10:
                            return "❌ Too many webpage redirects (limit 10)"
                        redirect_url = urljoin(url, location)
                        if urlparse(redirect_url).scheme not in {"http", "https"}:
                            return "❌ Only HTTP and HTTPS URLs are supported"
                        next_url, validation_error = await _validate_public_http_url(redirect_url)
                        if validation_error not in (None, "") or next_url is None:
                            return validation_error or "❌ Please provide a URL"
                        url = next_url
                        redirects += 1
                        continue

                    content_length = cast("str | None", resp.headers.get("content-length"))
                    if isinstance(content_length, str) and content_length.isdigit() and int(content_length) > max_bytes:
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
                    final_url = url
                    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                    encoding = resp.encoding or "utf-8"
                    break

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
            if soup.title is not None and soup.title.string not in (None, ""):
                title = str(soup.title.string).strip()
            meta_description = soup.find("meta", attrs={"name": "description"})
            meta_content = meta_description.get("content") if meta_description is not None else None
            if isinstance(meta_content, str) and meta_content:
                description = meta_content.strip()

            extracted_raw = extract_readable(
                text,
                url=final_url,
                output_format="markdown",
                include_links=include_links,
                include_comments=False,
                include_tables=True,
            )
            extracted = (
                extracted_raw
                if isinstance(extracted_raw, str) and extracted_raw
                else _fallback_extract_visible_text(text)
            )
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

    except OutboundPolicyError as exc:
        return f"❌ {exc}"
    except TimeoutException:
        return f"❌ Webpage fetch timed out: {url}"
    except Exception as e:
        return f"❌ Webpage read error: {str(e)[:300]}"
