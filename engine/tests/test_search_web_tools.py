from __future__ import annotations

import importlib
import socket
import sys
from collections import deque
from types import SimpleNamespace

from app.core.json_types import JsonObject, JsonValue
from app.services import agent_tools

type HttpCall = tuple[str, str, JsonObject]


def _web_read_module():
    try:
        return importlib.import_module("app.services.agent_tool_exec.web_read")
    except ModuleNotFoundError as exc:
        if exc.name == "app.services.agent_tool_exec.web_read":
            return agent_tools
        raise


class _HttpResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        url: str = "https://example.test/page",
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}
        self.encoding = encoding
        self._chunks = [text.encode(encoding)]

    async def __aenter__(self) -> _HttpResponse:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


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

    def stream(self, method: str, url: str, **kwargs: JsonValue) -> _HttpResponse:
        self._calls.append((method, url, {"client_kwargs": self._client_kwargs, **kwargs}))
        return self._responses.popleft()


class _FakeHttpxModule:
    TimeoutException = TimeoutError

    def __init__(self, *responses: _HttpResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[HttpCall] = []
        self.AsyncClient = self._make_client

    def _make_client(self, *args, **kwargs: JsonValue) -> _QueuedAsyncClient:
        del args
        return _QueuedAsyncClient(self.responses, self.calls, kwargs)


async def test_validate_public_http_url_preserves_ssrf_guard_and_proxy_test_exemption(monkeypatch) -> None:
    target = _web_read_module()
    dns_calls: list[tuple[str, int]] = []

    def fake_getaddrinfo(host: str, port: int, *, type: socket.SocketKind):
        assert type == socket.SOCK_STREAM
        dns_calls.append((host, port))
        if host == "public.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        if host == "proxy-range.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", port))]
        if host == "private.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        raise OSError("dns unavailable")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    public_url, public_error = await target._validate_public_http_url("public.example/path")
    proxy_url, proxy_error = await target._validate_public_http_url("https://proxy-range.example/read")
    direct_proxy_ip_url, direct_proxy_ip_error = await target._validate_public_http_url("https://198.18.0.1/read")
    localhost_url, localhost_error = await target._validate_public_http_url("http://localhost:8080")
    private_url, private_error = await target._validate_public_http_url("https://private.example")
    ftp_url, ftp_error = await target._validate_public_http_url("ftp://example.com/file")
    missing_url, missing_error = await target._validate_public_http_url("")
    unresolved_url, unresolved_error = await target._validate_public_http_url("https://missing.example")

    assert public_url == "https://public.example/path"
    assert public_error is None
    assert proxy_url == "https://proxy-range.example/read"
    assert proxy_error is None
    assert direct_proxy_ip_url is None
    assert direct_proxy_ip_error == "❌ Private, local, reserved, or internal network URLs are blocked (198.18.0.1)"
    assert localhost_url is None
    assert localhost_error == "❌ Localhost URLs are blocked for safety"
    assert private_url is None
    assert private_error == "❌ Private, local, reserved, or internal network URLs are blocked (10.0.0.5)"
    assert ftp_url is None
    assert ftp_error == "❌ Only HTTP and HTTPS URLs are supported"
    assert missing_url is None
    assert missing_error == "❌ Please provide a URL"
    assert unresolved_url is None
    assert unresolved_error == "❌ Could not resolve hostname missing.example: dns unavailable"
    assert dns_calls == [
        ("public.example", 443),
        ("proxy-range.example", 443),
        ("private.example", 443),
        ("missing.example", 443),
    ]


def test_fallback_extract_visible_text_removes_non_content_blocks() -> None:
    target = _web_read_module()
    html = (
        "<html><head><style>.x{}</style><script>alert(1)</script></head>"
        "<body><nav>Skip nav</nav><main><h1>Title</h1><p>First   paragraph</p><p>Second</p></main>"
        "<footer>Skip</footer></body></html>"
    )

    result = target._fallback_extract_visible_text(html)

    assert result == "Title\nFirst paragraph\nSecond"


def test_extract_page_links_deduplicates_absolutizes_and_limits() -> None:
    target = _web_read_module()
    html = (
        '<a href="/alpha"> Alpha page </a>'
        '<a href="https://example.test/alpha">Duplicate absolute</a>'
        '<a href="https://other.test/beta"><span>Beta</span> page</a>'
        '<a href="mailto:ops@example.test">Mail</a>'
    )

    result = target._extract_page_links(html, "https://example.test/root/index.html", limit=2)

    assert result == [
        "- Alpha page: https://example.test/alpha",
        "- Beta page: https://other.test/beta",
    ]


async def test_read_webpage_extracts_metadata_text_and_links_without_network(monkeypatch) -> None:
    target = _web_read_module()
    html = (
        '<html><head><title>Example Title</title><meta name="description" content="Example description"></head>'
        '<body><article><h1>Heading</h1><p>Readable body</p><a href="/next">Next link</a></article></body></html>'
    )
    httpx = _FakeHttpxModule(
        _HttpResponse(text=html, headers={"content-type": "text/html", "content-length": str(len(html))})
    )

    async def validate_url(_url: str) -> tuple[str, None]:
        return "https://example.test/page", None

    monkeypatch.setitem(sys.modules, "httpx", httpx)
    monkeypatch.setitem(
        sys.modules, "trafilatura", SimpleNamespace(extract=lambda *_args, **_kwargs: "# Heading\n\nReadable body")
    )
    monkeypatch.setattr(target, "_validate_public_http_url", validate_url)

    result = await target._read_webpage({"url": "example.test/page", "include_links": True, "max_chars": 500})

    assert result == (
        "🌐 **Webpage content**\n\n"
        "URL: https://example.test/page\n"
        "Status: HTTP 200\n"
        "Title: Example Title\n"
        "Description: Example description\n\n"
        "---\n\n"
        "# Heading\n\nReadable body\n\n"
        "---\n\n"
        "Links:\n- Next link: https://example.test/next"
    )
    assert httpx.calls == [
        (
            "GET",
            "https://example.test/page",
            {
                "client_kwargs": {"follow_redirects": True, "timeout": 15},
                "headers": {
                    "User-Agent": "MaraClawBot/1.0 (+https://maraclaw.ai) Mozilla/5.0",
                    "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, text/*;q=0.8, */*;q=0.5",
                },
            },
        )
    ]


async def test_read_tools_empty_argument_messages() -> None:
    target = _web_read_module()

    assert await target._validate_public_http_url("") == (None, "❌ Please provide a URL")
    assert await target._read_webpage({}) == "❌ Please provide a URL"
