from __future__ import annotations

import importlib
import socket
import sys
from collections import deque
from types import SimpleNamespace

import pytest

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
        self.closed = False
        self.body_read = False
        self.chunks_read = 0

    async def __aenter__(self) -> _HttpResponse:
        return self

    async def __aexit__(self, *_args) -> bool:
        self.closed = True
        return False

    async def aiter_bytes(self):
        self.body_read = True
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk


class _QueuedAsyncClient:
    def __init__(self, responses: deque[_HttpResponse], calls: list[HttpCall], client_kwargs: JsonObject) -> None:
        self._responses = responses
        self._calls = calls
        self._client_kwargs = client_kwargs
        self._previous: _HttpResponse | None = None

    async def __aenter__(self) -> _QueuedAsyncClient:
        return self

    async def __aexit__(self, *_args) -> bool:
        return False

    async def get(self, url: str, **kwargs: JsonValue) -> _HttpResponse:
        self._calls.append(("GET", url, {"client_kwargs": self._client_kwargs, **kwargs}))
        return self._responses.popleft()

    def stream(self, method: str, url: str, **kwargs: JsonValue) -> _HttpResponse:
        assert self._previous is None or self._previous.closed
        self._calls.append((method, url, {"client_kwargs": self._client_kwargs, **kwargs}))
        self._previous = self._responses.popleft()
        return self._previous


class _FakeHttpxModule:
    TimeoutException = TimeoutError

    def __init__(self, *responses: _HttpResponse) -> None:
        self.responses = deque(responses)
        self.calls: list[HttpCall] = []
        self.AsyncClient = self._make_client

    def _make_client(self, *args, **kwargs: JsonValue) -> _QueuedAsyncClient:
        del args
        return _QueuedAsyncClient(self.responses, self.calls, kwargs)


async def test_validate_public_http_url_rejects_private_and_benchmark_destinations(monkeypatch) -> None:
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
        raise socket.gaierror(socket.EAI_NONAME, "dns unavailable")

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
    assert proxy_url is None
    assert proxy_error == "❌ Private, local, reserved, or internal network URLs are blocked (198.18.0.1)"
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

    monkeypatch.setattr(target, "public_http_client", httpx.AsyncClient)
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
                "client_kwargs": {"follow_redirects": False, "timeout": 15},
                "headers": {
                    "User-Agent": "MaraClawBot/1.0 (+https://maraclaw.ai) Mozilla/5.0",
                    "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, text/*;q=0.8, */*;q=0.5",
                },
            },
        )
    ]


@pytest.fixture
def redirect_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def resolve(host: str, port: int, *, type: socket.SocketKind):
        assert type == socket.SOCK_STREAM
        calls.append(host)
        if host == "missing.example":
            raise socket.gaierror(socket.EAI_NONAME, "dns unavailable")
        addresses = {
            "private.example": "10.0.0.5",
            "proxy-range.example": "198.18.0.1",
        }
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses.get(host, "93.184.216.34"), port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return calls


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("https://other.example/final", "https://other.example/final"),
        ("../final", "https://public.example/final"),
        ("//other.example/final", "https://other.example/final"),
    ],
)
async def test_read_webpage_follows_validated_redirects(monkeypatch, redirect_dns, status, location, expected) -> None:
    # Given a two-hop chain whose response URLs cannot be trusted as redirect bases.
    first = _HttpResponse(status_code=status, headers={"location": "/dir/step"})
    second = _HttpResponse(status_code=status, headers={"location": location})
    final = _HttpResponse(
        text="Readable body", url="http://127.0.0.1/unvalidated", headers={"content-type": "text/plain"}
    )
    httpx = _FakeHttpxModule(first, second, final)
    monkeypatch.setattr(_web_read_module(), "public_http_client", httpx.AsyncClient)

    # When reading with the real URL validator and deterministic DNS.
    result = await _web_read_module()._read_webpage({"url": "https://public.example/start"})

    # Then only validated URLs were sent and all streams have been closed.
    assert [call[1] for call in httpx.calls] == [
        "https://public.example/start",
        "https://public.example/dir/step",
        expected,
    ]
    assert all(call[2]["client_kwargs"] == {"follow_redirects": False, "timeout": 15} for call in httpx.calls)
    assert f"URL: {expected}\n" in result
    assert result.endswith("Readable body")
    assert "127.0.0.1" not in result
    assert first.closed
    assert second.closed
    assert final.closed
    assert not first.body_read
    assert not second.body_read
    assert len(redirect_dns) == 3


@pytest.mark.parametrize(
    ("location", "error"),
    [
        ("http://127.0.0.1/", "blocked (127.0.0.1)"),
        ("http://[::1]/", "blocked (::1)"),
        ("http://169.254.169.254/", "blocked (169.254.169.254)"),
        ("http://10.0.0.5/", "blocked (10.0.0.5)"),
        ("http://localhost/", "Localhost URLs are blocked"),
        ("https://198.18.0.1/", "blocked (198.18.0.1)"),
        ("https://proxy-range.example/final", "blocked (198.18.0.1)"),
        ("ftp://public.example/file", "Only HTTP and HTTPS"),
        ("data:text/plain,secret", "Only HTTP and HTTPS"),
        ("mailto:ops@public.example", "Only HTTP and HTTPS"),
        ("https://private.example/", "blocked (10.0.0.5)"),
        ("https://missing.example/", "Could not resolve hostname missing.example"),
    ],
)
async def test_read_webpage_never_sends_forbidden_redirect(monkeypatch, redirect_dns, location, error) -> None:
    # Given a valid public hop before the forbidden target.
    first = _HttpResponse(status_code=302, headers={"location": "/step"})
    second = _HttpResponse(status_code=307, headers={"location": location})
    httpx = _FakeHttpxModule(first, second)
    monkeypatch.setattr(_web_read_module(), "public_http_client", httpx.AsyncClient)

    # When the later Location is resolved and validated.
    result = await _web_read_module()._read_webpage({"url": "https://public.example/start"})

    # Then the forbidden target never reaches the transport, including non-HTTP DNS.
    assert result.startswith("❌")
    assert error in result
    assert [call[1] for call in httpx.calls] == ["https://public.example/start", "https://public.example/step"]
    assert first.closed
    assert second.closed
    assert not first.body_read
    assert not second.body_read
    assert redirect_dns == ["public.example", "public.example"] + (
        ["private.example"]
        if "private.example" in location
        else ["missing.example"]
        if "missing.example" in location
        else ["proxy-range.example"]
        if "proxy-range.example" in location
        else []
    )


@pytest.mark.parametrize("cycle", [False, True])
async def test_read_webpage_bounds_redirect_chains(monkeypatch, redirect_dns, cycle) -> None:
    # Given enough redirects to exceed ten transitions.
    responses = [
        _HttpResponse(status_code=302, headers={"location": "/start" if cycle else f"/hop/{index + 1}"})
        for index in range(12)
    ]
    httpx = _FakeHttpxModule(*responses)
    monkeypatch.setattr(_web_read_module(), "public_http_client", httpx.AsyncClient)

    # When the last permitted request also redirects.
    result = await _web_read_module()._read_webpage({"url": "https://public.example/start"})

    # Then no twelfth request is sent and every opened response is closed.
    assert result == "❌ Too many webpage redirects (limit 10)"
    assert len(httpx.calls) == 11
    assert all(response.closed for response in responses[:11])
    assert not responses[11].closed


async def test_read_tools_empty_argument_messages() -> None:
    target = _web_read_module()

    assert await target._validate_public_http_url("") == (None, "❌ Please provide a URL")
    assert await target._read_webpage({}) == "❌ Please provide a URL"


async def test_read_webpage_rejects_declared_oversize_before_reading(monkeypatch, redirect_dns) -> None:
    response = _HttpResponse(text="unread", headers={"content-length": "2000001"})
    client = _FakeHttpxModule(response)
    monkeypatch.setattr(_web_read_module(), "public_http_client", client.AsyncClient)

    result = await _web_read_module()._read_webpage({"url": "https://public.example/page"})

    assert "Page is too large" in result
    assert response.closed
    assert not response.body_read


@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_read_webpage_caps_stream_before_extraction(monkeypatch, redirect_dns, extra_bytes) -> None:
    response = _HttpResponse(headers={"content-type": "text/html"})
    response._chunks = (
        [b"a" * 1_999_999, b"b" * (1 + extra_bytes), b"must not be consumed"]
        if extra_bytes
        else [b"a" * 1_999_999, b"b"]
    )
    client = _FakeHttpxModule(response)
    extracted_inputs: list[str] = []

    def extract(text: str, **_kwargs) -> str:
        extracted_inputs.append(text)
        return "Readable body"

    monkeypatch.setattr(_web_read_module(), "public_http_client", client.AsyncClient)
    monkeypatch.setitem(sys.modules, "trafilatura", SimpleNamespace(extract=extract))

    result = await _web_read_module()._read_webpage({"url": "https://public.example/page"})

    assert extracted_inputs == ["a" * 1_999_999 + "b"]
    assert ("response body truncated at 2000000 bytes" in result) == bool(extra_bytes)
    assert result.endswith("Readable body")
    assert response.closed
    assert response.chunks_read == 2


async def test_read_webpage_reports_connection_policy_errors(monkeypatch, redirect_dns) -> None:
    from app.core.outbound_policy import OutboundPolicyError

    def blocked_client(**_kwargs):
        raise OutboundPolicyError("Private, local, reserved, or internal network URLs are blocked (127.0.0.1)")

    monkeypatch.setattr(_web_read_module(), "public_http_client", blocked_client)

    result = await _web_read_module()._read_webpage({"url": "https://public.example/page"})

    assert result == "❌ Private, local, reserved, or internal network URLs are blocked (127.0.0.1)"
