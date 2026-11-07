"""Proxy client rotation and the request, response and deadline bounds."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, final, override
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api import tavily_proxy
from app.dao.tavily_proxy_dao import TavilyAgentAvailability
from app.records.tavily_api_key import TavilyApiKeyRecord
from app.services.tavily import client as client_mod, http_client
from app.services.tavily.client import TavilyProxyError, allowed_upstream_path, proxy_tavily
from app.services.tavily.errors import TavilyDeadlineExceededError, TavilyLimitError, TavilyResponseTooLargeError
from app.services.tavily.http_client import reset_tavily_client, shutdown_tavily_client, tavily_client
from app.services.tavily.keys import add_key
from app.services.tavily.tokens import _proxy_token, parse_proxy_token


class MemoryKeyDao:
    def __init__(self) -> None:
        self.rows: dict[UUID, TavilyApiKeyRecord] = {}
        self.cursor: UUID | None = None

    async def get(self, key_id: UUID) -> TavilyApiKeyRecord | None:
        return self.rows.get(key_id)

    async def get_by_fingerprint(self, fingerprint: str) -> TavilyApiKeyRecord | None:
        for row in self.rows.values():
            if row.key_fingerprint == fingerprint:
                return row
        return None

    async def is_empty(self) -> bool:
        return not self.rows

    async def max_position(self) -> int | None:
        if not self.rows:
            return None
        return max(row.position for row in self.rows.values())

    async def list_ordered(self) -> list[TavilyApiKeyRecord]:
        return sorted(self.rows.values(), key=lambda row: row.position)

    async def list_active_ordered(self, *, now) -> list[TavilyApiKeyRecord]:
        del now
        return sorted(
            [row for row in self.rows.values() if row.status == "active"],
            key=lambda row: row.position,
        )

    async def create(self, *, obj_in: dict[str, Any]) -> TavilyApiKeyRecord:
        record = TavilyApiKeyRecord(
            id=uuid4(),
            label=str(obj_in["label"]),
            key_ciphertext=str(obj_in["key_ciphertext"]),
            key_fingerprint=str(obj_in["key_fingerprint"]),
            position=int(obj_in["position"]),
            status="active",
        )
        self.rows[record.id] = record
        return record

    async def update(self, *, db_obj: TavilyApiKeyRecord, obj_in: dict[str, Any]) -> TavilyApiKeyRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def delete(self, *, record_id: UUID) -> TavilyApiKeyRecord | None:
        return self.rows.pop(record_id, None)

    async def get_cursor_key_id(self) -> UUID | None:
        return self.cursor

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        self.cursor = key_id


class FakeStream(httpx.AsyncByteStream):
    """Scripted upstream body: shows how far it was read and whether the proxy closed it."""

    def __init__(self, chunks: list[bytes], after_chunk: Callable[[], None] | None) -> None:
        self.chunks = chunks
        self.after_chunk = after_chunk
        self.sent = 0
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.sent += len(chunk)
            yield chunk
            if self.after_chunk is not None:
                self.after_chunk()

    @override
    async def aclose(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        text: str = "",
        *,
        chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        on_send: Callable[[], None] | None = None,
        after_chunk: Callable[[], None] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.stream = FakeStream(chunks if chunks is not None else ([text.encode()] if text else []), after_chunk)
        self.on_send = on_send

    def build(self) -> httpx.Response:
        if self.on_send is not None:
            self.on_send()
        return httpx.Response(self.status_code, headers=self.headers, stream=self.stream)


class FakeClient(httpx.AsyncClient):
    """A real httpx client over MockTransport, so streaming, decoding and redirects behave as in production."""

    def __init__(
        self,
        responses: list[FakeResponse],
        calls: list[tuple[str, str, str]],
        *,
        follow_redirects: bool = True,
    ) -> None:
        super().__init__(transport=httpx.MockTransport(self._respond), follow_redirects=follow_redirects)
        self._responses = responses
        self._calls = calls

    def _respond(self, request: httpx.Request) -> httpx.Response:
        self._calls.append((request.method, str(request.url), request.headers.get("authorization", "")))
        return self._responses.pop(0).build()


class FakeClock:
    """Stands in for ``time.monotonic``: time only moves when a test or a fake upstream advances it."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RingSpy:
    """Counts the key-ring side effects a local limit failure must never cause."""

    def __init__(self) -> None:
        self.exhausted: list[UUID] = []
        self.advanced: list[UUID] = []
        self.on_advance: Callable[[], None] | None = None


def proxy_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "SECRET_KEY": "test-secret",
        "TAVILY_API_KEY": "",
        "TAVILY_PROXY_MAX_REQUEST_BYTES": 1024,
        "TAVILY_PROXY_MAX_RESPONSE_BYTES": 1024,
        "TAVILY_PROXY_DEADLINE_SECONDS": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def use_upstream(monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(client_mod, "tavily_client", lambda: FakeClient(responses, calls))
    return calls


@pytest.fixture
def limits() -> SimpleNamespace:
    return proxy_settings()


@pytest.fixture
def recorded() -> list[dict[str, object]]:
    return []


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(client_mod, "_monotonic", fake)
    return fake


@pytest.fixture
def ring(monkeypatch: pytest.MonkeyPatch, limits: SimpleNamespace, recorded: list[dict[str, object]]) -> MemoryKeyDao:
    from app.services.tavily import keys as keys_mod

    store = MemoryKeyDao()
    monkeypatch.setattr(keys_mod, "tavily_api_key_dao", store)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: limits)
    monkeypatch.setattr(client_mod, "get_settings", lambda: limits)

    async def _passthrough_record(
        result: tuple[int, str, dict[str, str]], **kwargs: object
    ) -> tuple[int, str, dict[str, str]]:
        recorded.append({"result": result, **kwargs})
        return result

    monkeypatch.setattr(client_mod, "_record_result", _passthrough_record)
    return store


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> RingSpy:
    watcher = RingSpy()
    original_mark = client_mod.mark_exhausted
    original_advance = client_mod.advance_cursor

    async def mark(key_id: UUID, *, message: str, until: datetime | None = None) -> None:
        watcher.exhausted.append(key_id)
        await original_mark(key_id, message=message, until=until)

    async def advance(from_key_id: UUID) -> TavilyApiKeyRecord | None:
        watcher.advanced.append(from_key_id)
        following = await original_advance(from_key_id)
        if watcher.on_advance is not None:
            watcher.on_advance()
        return following

    monkeypatch.setattr(client_mod, "mark_exhausted", mark)
    monkeypatch.setattr(client_mod, "advance_cursor", advance)
    return watcher


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "quota_status"), [("search", 432), ("search", 433), ("crawl", 432), ("crawl", 433)])
async def test_proxy_rotates_on_quota_and_succeeds_on_next_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch, path: str, quota_status: int
) -> None:
    from app.core.security import decrypt_data
    from app.services.tavily import client as client_mod

    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(quota_status, '{"detail":{"error":"This request exceeds your plan\'s set usage limit."}}'),
        FakeResponse(200, '{"ok":true}'),
    ]
    monkeypatch.setattr(
        client_mod,
        "tavily_client",
        lambda: FakeClient(responses, calls),
    )

    status, body, _headers = await proxy_tavily(
        method="POST",
        path=path,
        headers={},
        content=b'{"query":"x"}',
    )
    assert status == 200
    assert body == '{"ok":true}'
    assert decrypt_data(first.key_ciphertext, "test-secret") == "key-a"
    assert "Bearer key-a" in calls[0][2]
    assert "Bearer key-b" in calls[1][2]
    assert first.exhausted_until is not None
    assert ring.cursor == second.id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 429, 434])
async def test_proxy_passes_non_quota_client_errors_through_without_rotating(
    ring: MemoryKeyDao, spy: RingSpy, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    calls = use_upstream(
        monkeypatch, [FakeResponse(status, '{"detail":{"error":"slow down"}}'), FakeResponse(200, "{}")]
    )

    result = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert result[0] == status
    assert [auth for _m, _u, auth in calls] == ["Bearer key-a"]
    _assert_ring_untouched(ring, spy, first, second)


@pytest.mark.asyncio
async def test_proxy_does_not_rotate_on_500_then_retries_same_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.tavily import client as client_mod

    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(500, "boom"),
        FakeResponse(200, '{"ok":true}'),
    ]
    monkeypatch.setattr(client_mod, "tavily_client", lambda: FakeClient(responses, calls))
    status, body, _headers = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert status == 200
    assert body == '{"ok":true}'
    assert [auth for _m, _u, auth in calls] == ["Bearer key-a", "Bearer key-a"]


@pytest.mark.asyncio
async def test_empty_ring_returns_503(ring: MemoryKeyDao) -> None:
    _ = ring
    with pytest.raises(TavilyProxyError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.status_code == 503


def test_path_allowlist_and_proxy_token() -> None:
    assert allowed_upstream_path("search") is True
    assert allowed_upstream_path("/crawl") is True
    for rejected in (
        "research",
        "extract",
        "map",
        "research/123e4567-e89b-12d3-a456-426614174111",
        "admin",
        "fetch",
        "extract/job-1",
        "search/anything",
        "research/",
        "research/a/b",
        "research/..",
        "research/a?x=1",
        "research/a%2Fb",
    ):
        assert allowed_upstream_path(rejected) is False, rejected
    agent_id = uuid4()
    token = _proxy_token(agent_id, secret="s")
    assert parse_proxy_token(token, secret="s") == agent_id
    assert parse_proxy_token(token, secret="other") is None
    assert parse_proxy_token("not-a-token", secret="s") is None
    assert parse_proxy_token(f"{agent_id}.short", secret="s") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"), [("GET", "search"), ("GET", "crawl"), ("PUT", "search"), ("DELETE", "crawl")]
)
async def test_method_must_match_the_endpoint(ring: MemoryKeyDao, method: str, path: str) -> None:
    _ = ring
    with pytest.raises(TavilyProxyError) as exc:
        await proxy_tavily(method=method, path=path, headers={}, content=None)
    assert exc.value.status_code == 405


def _assert_ring_untouched(ring: MemoryKeyDao, spy: RingSpy, *keys: TavilyApiKeyRecord) -> None:
    assert spy.exhausted == []
    assert spy.advanced == []
    assert ring.cursor == keys[0].id
    assert all(key.exhausted_until is None for key in keys)


@pytest.mark.asyncio
async def test_response_ceiling_crossed_mid_stream_aborts_with_502(
    ring: MemoryKeyDao,
    limits: SimpleNamespace,
    spy: RingSpy,
    recorded: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits.TAVILY_PROXY_MAX_RESPONSE_BYTES = 10
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    oversized = FakeResponse(200, chunks=[b"123456", b"789012", b"never-read"])
    healthy = FakeResponse(200, '{"ok":true}')
    calls = use_upstream(monkeypatch, [oversized, healthy])

    with pytest.raises(TavilyResponseTooLargeError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert not isinstance(exc.value, TavilyProxyError)
    assert exc.value.status_code == 502
    assert exc.value.bytes_read == 12
    assert oversized.stream.sent == 12
    assert oversized.stream.closed
    assert len(calls) == 1
    _assert_ring_untouched(ring, spy, first, second)
    assert [entry["local_failure"] for entry in recorded] == [exc.value]


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "allowed"), [(10, True), (11, False)])
async def test_response_ceiling_is_inclusive(
    ring: MemoryKeyDao, limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, size: int, allowed: bool
) -> None:
    _ = ring
    limits.TAVILY_PROXY_MAX_RESPONSE_BYTES = 10
    _ = await add_key(label="a", api_key="key-a")
    _ = use_upstream(monkeypatch, [FakeResponse(200, "x" * size)])
    if allowed:
        status, body, _headers = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
        assert (status, body) == (200, "x" * size)
    else:
        with pytest.raises(TavilyResponseTooLargeError):
            await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")


@pytest.mark.asyncio
async def test_response_ceiling_counts_decoded_bytes(
    ring: MemoryKeyDao, limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = ring
    limits.TAVILY_PROXY_MAX_RESPONSE_BYTES = 50
    _ = await add_key(label="a", api_key="key-a")
    squeezed = gzip.compress(b"a" * 400)
    assert len(squeezed) < 50
    _ = use_upstream(monkeypatch, [FakeResponse(200, chunks=[squeezed], headers={"content-encoding": "gzip"})])
    with pytest.raises(TavilyResponseTooLargeError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.bytes_read == 400


@pytest.mark.asyncio
async def test_deadline_expiring_mid_stream_aborts_with_504(
    ring: MemoryKeyDao,
    spy: RingSpy,
    clock: FakeClock,
    recorded: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    slow = FakeResponse(200, chunks=[b"abc", b"def", b"ghi", b"never-read"], after_chunk=lambda: clock.advance(20))
    calls = use_upstream(monkeypatch, [slow, FakeResponse(200, "{}")])

    with pytest.raises(TavilyDeadlineExceededError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert not isinstance(exc.value, TavilyProxyError)
    assert exc.value.status_code == 504
    assert exc.value.bytes_read == 9
    assert slow.stream.sent == 9
    assert slow.stream.closed
    assert len(calls) == 1
    _assert_ring_untouched(ring, spy, first, second)
    assert [entry["local_failure"] for entry in recorded] == [exc.value]


@pytest.mark.asyncio
async def test_deadline_expiring_during_rotation_stops_before_next_key(
    ring: MemoryKeyDao,
    spy: RingSpy,
    clock: FakeClock,
    recorded: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    spy.on_advance = lambda: clock.advance(31)
    calls = use_upstream(monkeypatch, [FakeResponse(432, '{"detail":{"error":"plan limit"}}'), FakeResponse(200, "{}")])

    with pytest.raises(TavilyDeadlineExceededError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert exc.value.bytes_read == 0
    assert len(calls) == 1
    # The quota rotation that happened inside the budget stands; nothing local touched the ring afterwards.
    assert spy.exhausted == [first.id]
    assert spy.advanced == [first.id]
    assert ring.cursor == second.id
    assert second.exhausted_until is None
    assert second.last_used_at is None
    assert recorded[0]["key_id"] == first.id
    assert recorded[0]["quota_rotated"] is True
    assert recorded[0]["local_failure"] is exc.value


@pytest.mark.asyncio
async def test_one_deadline_spans_the_transient_retry(
    ring: MemoryKeyDao,
    limits: SimpleNamespace,
    spy: RingSpy,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits.TAVILY_PROXY_DEADLINE_SECONDS = 120.0
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    calls = use_upstream(
        monkeypatch,
        [
            FakeResponse(500, "boom", on_send=lambda: clock.advance(100)),
            FakeResponse(200, '{"ok":true}', on_send=lambda: clock.advance(30)),
        ],
    )

    with pytest.raises(TavilyDeadlineExceededError):
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert [auth for _m, _u, auth in calls] == ["Bearer key-a", "Bearer key-a"]
    _assert_ring_untouched(ring, spy, first, second)
    assert second.last_used_at is None


@pytest.mark.asyncio
async def test_deadline_spent_before_the_retry_does_not_resend(
    ring: MemoryKeyDao,
    spy: RingSpy,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await add_key(label="a", api_key="key-a")
    calls = use_upstream(
        monkeypatch,
        [FakeResponse(500, "boom", on_send=lambda: clock.advance(31)), FakeResponse(200, "{}")],
    )
    with pytest.raises(TavilyDeadlineExceededError):
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert len(calls) == 1
    _assert_ring_untouched(ring, spy, first)


@pytest.mark.asyncio
async def test_deadline_covers_the_env_key_fallback(
    ring: MemoryKeyDao,
    limits: SimpleNamespace,
    spy: RingSpy,
    clock: FakeClock,
    recorded: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits.TAVILY_API_KEY = "env-key"
    stored = await add_key(label="a", api_key="key-a")
    stored.status = "disabled"
    slow = FakeResponse(200, chunks=[b"abc", b"def"], after_chunk=lambda: clock.advance(31))
    calls = use_upstream(monkeypatch, [slow])

    with pytest.raises(TavilyDeadlineExceededError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert [auth for _m, _u, auth in calls] == ["Bearer env-key"]
    assert slow.stream.closed
    assert spy.exhausted == []
    assert spy.advanced == []
    assert recorded[0]["key_id"] is None
    assert recorded[0]["local_failure"] is exc.value


@pytest.mark.asyncio
async def test_redirects_are_still_followed(ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    calls = use_upstream(
        monkeypatch,
        [
            FakeResponse(302, headers={"location": "https://api.tavily.com/search/moved"}),
            FakeResponse(200, '{"ok":true}'),
        ],
    )
    status, body, _headers = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert (status, body) == (200, '{"ok":true}')
    assert [(method, url) for method, url, _auth in calls] == [
        ("POST", "https://api.tavily.com/search"),
        ("GET", "https://api.tavily.com/search/moved"),
    ]
    assert {auth for _m, _u, auth in calls} == {"Bearer key-a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 302, 307])
async def test_response_ceiling_applies_before_redirect_following_or_success(
    ring: MemoryKeyDao, limits: SimpleNamespace, spy: RingSpy, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    limits.TAVILY_PROXY_MAX_RESPONSE_BYTES = 10
    first = await add_key(label="a", api_key="key-a")
    oversized = FakeResponse(status, chunks=[b"123456", b"789012", b"not read"], headers={"location": "/search/next"})
    calls = use_upstream(monkeypatch, [oversized, FakeResponse(200, "{}")])

    with pytest.raises(TavilyResponseTooLargeError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert exc.value.bytes_read == 12
    assert oversized.stream.sent == 12
    assert oversized.stream.closed
    assert len(calls) == 1
    _assert_ring_untouched(ring, spy, first)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "method", "authorization"), [(307, "POST", ""), (302, "GET", "Bearer key-a")])
async def test_redirect_request_keeps_httpx_method_and_auth_rules(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch, status: int, method: str, authorization: str
) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    url = "https://other.example/next" if status == 307 else "https://api.tavily.com/next"
    calls = use_upstream(
        monkeypatch, [FakeResponse(status, "redirect body", headers={"location": url}), FakeResponse(200, "{}")]
    )
    result = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert result[0] == 200
    assert calls[-1] == (method, url, authorization)


@pytest.mark.asyncio
async def test_transport_failure_is_still_a_proxy_error_not_a_limit(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")

    class Refusing(FakeClient):
        @override
        def _respond(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(client_mod, "tavily_client", lambda: Refusing([], []))
    with pytest.raises(TavilyProxyError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.status_code == 502


def _route_app(monkeypatch: pytest.MonkeyPatch, limits: SimpleNamespace, upstream: AsyncMock) -> FastAPI:
    agent_id = uuid4()
    monkeypatch.setattr(tavily_proxy, "parse_proxy_token", lambda _token: agent_id)
    monkeypatch.setattr(
        tavily_proxy, "agent_available", AsyncMock(return_value=TavilyAgentAvailability(tenant_id=uuid4()))
    )
    monkeypatch.setattr(tavily_proxy, "get_settings", lambda: limits)
    monkeypatch.setattr(tavily_proxy, "proxy_tavily", upstream)
    app = FastAPI()
    app.include_router(tavily_proxy.router)

    @app.post("/other")
    async def other(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    return app


async def _post(app: FastAPI, path: str, content: bytes | AsyncIterator[bytes]) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test") as client:
        request = client.build_request("POST", path, headers={"Authorization": "Bearer agent.digest"}, content=content)
        return await client.send(request)


def _chunked(sizes: list[int], consumed: list[int]) -> AsyncIterator[bytes]:
    async def generate() -> AsyncIterator[bytes]:
        for size in sizes:
            consumed.append(size)
            yield b"x" * size

    return generate()


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled_at", ["lookup", "stream"])
async def test_deadline_cancels_a_stalled_operation_without_sleep(
    ring: MemoryKeyDao,
    spy: RingSpy,
    recorded: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    stalled_at: str,
) -> None:
    first = await add_key(label="a", api_key="key-a")
    original_timeout = asyncio.timeout
    scopes: list[asyncio.Timeout] = []
    closed: list[bool] = []

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        scope = original_timeout(delay)
        scopes.append(scope)
        return scope

    async def stall() -> None:
        scopes[0].reschedule(asyncio.get_running_loop().time())
        await asyncio.Event().wait()

    class StalledStream(httpx.AsyncByteStream):
        @override
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"abc"
            await stall()

        @override
        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(client_mod.asyncio, "timeout", capture_timeout)
    if stalled_at == "lookup":
        monkeypatch.setattr(client_mod, "current_key", stall)
    else:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=StalledStream()))
        monkeypatch.setattr(client_mod, "tavily_client", lambda: httpx.AsyncClient(transport=transport))

    with pytest.raises(TavilyDeadlineExceededError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert exc.value.bytes_read == (0 if stalled_at == "lookup" else 3)
    assert len(scopes) == 1
    assert closed == ([] if stalled_at == "lookup" else [True])
    assert [entry["local_failure"] for entry in recorded] == [exc.value]
    _assert_ring_untouched(ring, spy, first)


@pytest.mark.asyncio
async def test_route_rejects_chunked_body_over_the_ceiling_while_reading(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits.TAVILY_PROXY_MAX_REQUEST_BYTES = 1024
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    consumed: list[int] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test") as client:
        request = client.build_request(
            "POST",
            "/api/tavily/v1/search",
            headers={"Authorization": "Bearer agent.digest"},
            content=_chunked([600] * 4, consumed),
        )
        assert "content-length" not in request.headers
        response = await client.send(request)
    assert response.status_code == 413
    assert len(consumed) < 4
    upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("sizes", "status"), [([512, 512], 200), ([512, 513], 413), ([1024], 200), ([1025], 413)])
async def test_route_body_ceiling_is_exact(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, sizes: list[int], status: int
) -> None:
    limits.TAVILY_PROXY_MAX_REQUEST_BYTES = 1024
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    response = await _post(app, "/api/tavily/v1/search", _chunked(sizes, []))
    assert response.status_code == status
    if status == 200:
        assert upstream.await_args is not None
        assert upstream.await_args.kwargs["content"] == b"x" * sum(sizes)
    else:
        upstream.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "status"), [(1024, 200), (1025, 413)])
async def test_route_rejects_declared_content_length_over_the_ceiling(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, size: int, status: int
) -> None:
    limits.TAVILY_PROXY_MAX_REQUEST_BYTES = 1024
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    response = await _post(app, "/api/tavily/v1/search", b"x" * size)
    assert response.status_code == status
    assert upstream.await_count == (1 if status == 200 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_path", "retired_fetch", "extract_poll", "bad_token", "agent_unavailable"])
async def test_route_validates_path_and_token_before_reading_the_body(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    if failure == "bad_token":
        monkeypatch.setattr(tavily_proxy, "parse_proxy_token", lambda _token: None)
    if failure == "agent_unavailable":
        monkeypatch.setattr(tavily_proxy, "agent_available", AsyncMock(return_value=None))
    consumed: list[int] = []
    paths = {
        "unknown_path": "/api/tavily/v1/admin",
        "retired_fetch": "/api/tavily/v1/fetch",
        "extract_poll": "/api/tavily/v1/extract/job-1",
    }
    path = paths.get(failure, "/api/tavily/v1/search")
    response = await _post(app, path, _chunked([600] * 4, consumed))
    assert response.status_code == (401 if failure in {"bad_token", "agent_unavailable"} else 404)
    assert consumed == []
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_limit_does_not_cap_other_routes(limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    limits.TAVILY_PROXY_MAX_REQUEST_BYTES = 1024
    app = _route_app(monkeypatch, limits, AsyncMock(return_value=(200, "{}", {})))
    response = await _post(app, "/other", b"x" * 4096)
    assert response.status_code == 200
    assert response.json() == {"bytes": 4096}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    [(TavilyResponseTooLargeError(bytes_read=5), 502), (TavilyDeadlineExceededError(), 504)],
)
async def test_route_returns_the_local_failure_status(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: TavilyLimitError, status: int
) -> None:
    app = _route_app(monkeypatch, limits, AsyncMock(side_effect=failure))
    response = await _post(app, "/api/tavily/v1/search", b"{}")
    assert response.status_code == status
    assert response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_id", [None, uuid4()])
async def test_route_hands_the_available_agents_tenant_to_the_client(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tenant_id: UUID | None
) -> None:
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    availability = AsyncMock(return_value=TavilyAgentAvailability(tenant_id=tenant_id))
    monkeypatch.setattr(tavily_proxy, "agent_available", availability)

    response = await _post(app, "/api/tavily/v1/search", b"{}")

    assert response.status_code == 200
    assert upstream.await_args is not None
    assert upstream.await_args.kwargs["tenant_id"] == tenant_id


@final
class SharedWire:
    """The real shared client over an injected transport: what was built, and what reached the wire."""

    def __init__(self) -> None:
        self.built: list[dict[str, object]] = []
        self.requests: list[httpx.Request] = []
        self.responses: list[FakeResponse] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0).build()


@pytest.fixture
async def shared_wire(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SharedWire]:
    wire = SharedWire()
    original = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        wire.built.append(kwargs)
        return original(transport=httpx.MockTransport(wire.respond), **kwargs)

    reset_tavily_client()
    monkeypatch.setattr(http_client.httpx, "AsyncClient", factory)
    yield wire
    await shutdown_tavily_client()
    reset_tavily_client()


@pytest.mark.asyncio
async def test_every_attempt_and_later_call_shares_one_open_client(ring: MemoryKeyDao, shared_wire: SharedWire) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    shared_wire.responses = [
        FakeResponse(500, "boom"),
        FakeResponse(432, '{"detail":{"error":"plan limit"}}'),
        FakeResponse(200, '{"results":[]}'),
        FakeResponse(200, "{}"),
    ]

    first = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    second = await proxy_tavily(method="POST", path="crawl", headers={}, content=b"{}")

    assert (first[0], second[0]) == (200, 200)
    assert [request.headers["authorization"] for request in shared_wire.requests] == [
        "Bearer key-a",
        "Bearer key-a",
        "Bearer key-b",
        "Bearer key-b",
    ]
    assert len(shared_wire.built) == 1
    assert shared_wire.built[0]["follow_redirects"] is False
    limits = shared_wire.built[0]["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.max_connections is not None
    assert limits.max_keepalive_connections is not None
    assert not tavily_client().is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("search", b'{"query":"stripe ceo","search_depth":"advanced","max_results":3}'),
        ("crawl", b'{"url":"https://example.com/a","extract_depth":"basic"}'),
    ],
)
async def test_upstream_request_is_the_unprefixed_tavily_url_with_the_payload_and_ring_key(
    ring: MemoryKeyDao, shared_wire: SharedWire, path: str, payload: bytes
) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="tvly-key-a")
    shared_wire.responses = [FakeResponse(200, '{"results":[],"failed_results":[],"request_id":"r"}')]

    status, body, _headers = await proxy_tavily(
        method="POST",
        path=path,
        headers={"content-type": "application/json", "authorization": "Bearer tvly-guest-token"},
        content=payload,
    )

    assert status == 200
    assert body == '{"results":[],"failed_results":[],"request_id":"r"}'
    request = shared_wire.requests[0]
    assert (request.method, str(request.url)) == ("POST", f"https://api.tavily.com/{path}")
    assert request.content == payload
    assert request.headers["authorization"] == "Bearer tvly-key-a"
    assert request.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_authenticated_http_boundary_rotates_and_forwards_search_and_crawl(
    ring: MemoryKeyDao, limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hashlib import sha256

    from app.services.tavily import tokens

    first = await add_key(label="first", api_key="upstream-first")
    second = await add_key(label="second", api_key="upstream-second")
    agent = uuid4()
    token = _proxy_token(agent, secret=limits.SECRET_KEY)
    availability = AsyncMock(return_value=TavilyAgentAvailability(tenant_id=None))
    monkeypatch.setattr(tokens, "get_settings", lambda: limits)
    monkeypatch.setattr(tavily_proxy, "agent_available", availability)
    monkeypatch.setattr(tavily_proxy, "get_settings", lambda: limits)
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(432, json={"detail": {"error": "plan limit"}})
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"url": "https://example.org", "content": "found"}]})
        return httpx.Response(
            200,
            json={
                "base_url": "https://example.org",
                "results": [{"url": "https://example.org", "raw_content": "page"}],
                "response_time": 1,
                "request_id": "r",
            },
        )

    app = FastAPI()
    app.include_router(tavily_proxy.router)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        monkeypatch.setattr(client_mod, "tavily_client", lambda: outbound)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine") as guest:
            denied = await guest.post("/api/tavily/v1/search", headers={"Authorization": "Bearer invalid"})
            research = await guest.post(
                "/api/tavily/v1/research", headers={"Authorization": f"Bearer {token}"}, json={"input": "x"}
            )
            poll = await guest.get("/api/tavily/v1/research/job-1", headers={"Authorization": f"Bearer {token}"})
            extract = await guest.post("/api/tavily/v1/extract", headers={"Authorization": f"Bearer {token}"})
            map_response = await guest.post("/api/tavily/v1/map", headers={"Authorization": f"Bearer {token}"})
            search = await guest.post(
                "/api/tavily/v1/search", headers={"Authorization": f"Bearer {token}"}, json={"query": "question"}
            )
            crawl = await guest.post(
                "/api/tavily/v1/crawl",
                headers={"Authorization": f"Bearer {token}"},
                json={"url": "https://example.org"},
            )

    assert denied.status_code == 401
    assert research.status_code == 404
    assert poll.status_code == 404
    assert extract.status_code == 404
    assert map_response.status_code == 404
    assert search.json()["results"][0]["content"] == "found"
    assert crawl.json()["results"][0]["raw_content"] == "page"
    assert [str(request.url) for request in requests] == [
        "https://api.tavily.com/search",
        "https://api.tavily.com/search",
        "https://api.tavily.com/crawl",
    ]
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer upstream-first",
        "Bearer upstream-second",
        "Bearer upstream-second",
    ]
    assert requests[0].content == requests[1].content == b'{"query":"question"}'
    assert requests[2].content == b'{"url":"https://example.org"}'
    assert first.exhausted_until is not None
    assert ring.cursor == second.id
    assert availability.await_args_list[0].args == (agent, sha256(token.encode()).digest())


@pytest.mark.asyncio
async def test_client_is_replaced_when_the_loop_changes_or_it_is_closed(shared_wire: SharedWire) -> None:
    first = tavily_client()
    assert tavily_client() is first

    def on_another_loop() -> httpx.AsyncClient:
        async def build() -> httpx.AsyncClient:
            return tavily_client()

        return asyncio.run(build())

    second = await asyncio.to_thread(on_another_loop)
    assert second is not first
    third = tavily_client()
    assert third is not second
    assert tavily_client() is third

    await third.aclose()
    fourth = tavily_client()
    assert fourth is not third
    assert len(shared_wire.built) == 4


@pytest.mark.asyncio
async def test_shutdown_closes_the_client_and_is_idempotent(shared_wire: SharedWire) -> None:
    _ = shared_wire
    client = tavily_client()

    await shutdown_tavily_client()
    await shutdown_tavily_client()

    assert client.is_closed
    assert tavily_client() is not client


@pytest.mark.asyncio
async def test_set_cookie_is_never_replayed_on_a_later_call_retry_or_redirect(
    ring: MemoryKeyDao, shared_wire: SharedWire
) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    cookie = {"set-cookie": "sid=agent-a; Path=/; Domain=api.tavily.com"}
    shared_wire.responses = [
        FakeResponse(302, headers={"location": "https://api.tavily.com/search/moved", **cookie}),
        FakeResponse(500, "boom", headers=cookie),
        FakeResponse(200, "{}", headers=cookie),
        FakeResponse(200, "{}"),
    ]

    for _ in range(2):
        status, _body, _headers = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
        assert status == 200

    assert len(shared_wire.requests) == 4
    assert [request.headers.get("cookie") for request in shared_wire.requests] == [None] * 4
    assert list(tavily_client().cookies.jar) == []


@pytest.mark.asyncio
async def test_each_hop_carries_a_timeout_that_shrinks_with_the_deadline(
    ring: MemoryKeyDao, clock: FakeClock, shared_wire: SharedWire
) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    shared_wire.responses = [
        FakeResponse(
            302, headers={"location": "https://api.tavily.com/search/moved"}, on_send=lambda: clock.advance(10)
        ),
        FakeResponse(200, "{}", on_send=lambda: clock.advance(1)),
    ]

    status, _body, _headers = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert status == 200
    timeouts = [request.extensions["timeout"] for request in shared_wire.requests]
    assert [(t["connect"], t["read"], t["write"], t["pool"]) for t in timeouts] == [(30.0,) * 4, (20.0,) * 4]


@pytest.mark.asyncio
async def test_phase_timeout_caps_the_per_attempt_budget(
    ring: MemoryKeyDao, limits: SimpleNamespace, shared_wire: SharedWire
) -> None:
    _ = ring
    limits.TAVILY_PROXY_DEADLINE_SECONDS = 600.0
    _ = await add_key(label="a", api_key="key-a")
    shared_wire.responses = [FakeResponse(200, "{}")]

    _ = await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")

    assert shared_wire.requests[0].extensions["timeout"]["pool"] == client_mod._PHASE_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_pool_timeout_comes_back_as_the_proxys_502(ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")

    class Saturated(FakeClient):
        @override
        def _respond(self, request: httpx.Request) -> httpx.Response:
            raise httpx.PoolTimeout("no free connection", request=request)

    monkeypatch.setattr(client_mod, "tavily_client", lambda: Saturated([], []))
    with pytest.raises(TavilyProxyError) as exc:
        await proxy_tavily(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.status_code == 502
