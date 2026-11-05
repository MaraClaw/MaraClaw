"""Proxy client rotation, async job binding, and the request, response and deadline bounds."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, override
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api import linkup_proxy
from app.records.linkup_api_key import LinkupApiKeyRecord
from app.services.linkup import client as client_mod
from app.services.linkup.client import LinkupProxyError, allowed_upstream_path, proxy_linkup
from app.services.linkup.errors import LinkupDeadlineExceededError, LinkupLimitError, LinkupResponseTooLargeError
from app.services.linkup.jobs import LinkupJobKeyRemovedError, bind_job, key_for_job
from app.services.linkup.keys import add_key
from app.services.linkup.tokens import _proxy_token, parse_proxy_token


class MemoryKeyDao:
    def __init__(self) -> None:
        self.rows: dict[UUID, LinkupApiKeyRecord] = {}
        self.cursor: UUID | None = None

    async def get(self, key_id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.get(key_id)

    async def get_by_fingerprint(self, fingerprint: str) -> LinkupApiKeyRecord | None:
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

    async def list_ordered(self) -> list[LinkupApiKeyRecord]:
        return sorted(self.rows.values(), key=lambda row: row.position)

    async def list_active_ordered(self, *, now) -> list[LinkupApiKeyRecord]:
        del now
        return sorted(
            [row for row in self.rows.values() if row.status == "active"],
            key=lambda row: row.position,
        )

    async def create(self, *, obj_in: dict[str, Any]) -> LinkupApiKeyRecord:
        record = LinkupApiKeyRecord(
            id=uuid4(),
            label=str(obj_in["label"]),
            key_ciphertext=str(obj_in["key_ciphertext"]),
            key_fingerprint=str(obj_in["key_fingerprint"]),
            position=int(obj_in["position"]),
            status="active",
        )
        self.rows[record.id] = record
        return record

    async def update(self, *, db_obj: LinkupApiKeyRecord, obj_in: dict[str, Any]) -> LinkupApiKeyRecord:
        for key, value in obj_in.items():
            setattr(db_obj, key, value)
        return db_obj

    async def delete(self, *, record_id: UUID) -> LinkupApiKeyRecord | None:
        return self.rows.pop(record_id, None)

    async def get_cursor_key_id(self) -> UUID | None:
        return self.cursor

    async def set_cursor_key_id(self, key_id: UUID | None) -> None:
        self.cursor = key_id


class MemoryJobDao:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def get_by_job_id(self, upstream_job_id: str):
        return self.rows.get(upstream_job_id)

    async def create(self, *, obj_in: dict[str, Any]):
        from app.records.linkup_api_key import LinkupAsyncJobRecord

        record = LinkupAsyncJobRecord(
            upstream_job_id=str(obj_in["upstream_job_id"]),
            key_id=obj_in["key_id"],
            kind=str(obj_in["kind"]),
        )
        self.rows[record.upstream_job_id] = record
        return record


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
        "LINKUP_API_KEY": "",
        "LINKUP_PROXY_MAX_REQUEST_BYTES": 1024,
        "LINKUP_PROXY_MAX_RESPONSE_BYTES": 1024,
        "LINKUP_PROXY_DEADLINE_SECONDS": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def use_upstream(monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: FakeClient(responses, calls))
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
    from app.services.linkup import jobs as jobs_mod, keys as keys_mod

    store = MemoryKeyDao()
    jobs = MemoryJobDao()
    monkeypatch.setattr(keys_mod, "linkup_api_key_dao", store)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: limits)
    monkeypatch.setattr(jobs_mod, "linkup_api_key_dao", store)
    monkeypatch.setattr(jobs_mod, "linkup_async_job_dao", jobs)
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

    async def advance(from_key_id: UUID) -> LinkupApiKeyRecord | None:
        watcher.advanced.append(from_key_id)
        following = await original_advance(from_key_id)
        if watcher.on_advance is not None:
            watcher.on_advance()
        return following

    monkeypatch.setattr(client_mod, "mark_exhausted", mark)
    monkeypatch.setattr(client_mod, "advance_cursor", advance)
    return watcher


@pytest.mark.asyncio
async def test_proxy_rotates_on_quota_and_succeeds_on_next_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.security import decrypt_data
    from app.services.linkup import client as client_mod

    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(429, '{"error":"insufficient quota"}'),
        FakeResponse(200, '{"ok":true}'),
    ]
    monkeypatch.setattr(
        client_mod,
        "_httpx_client",
        lambda *a, **k: FakeClient(responses, calls),
    )

    status, body, _headers = await proxy_linkup(
        method="POST",
        path="search",
        headers={},
        content=b'{"q":"x"}',
    )
    assert status == 200
    assert body == '{"ok":true}'
    assert decrypt_data(first.key_ciphertext, "test-secret") == "key-a"
    assert "Bearer key-a" in calls[0][2]
    assert "Bearer key-b" in calls[1][2]
    assert first.exhausted_until is not None
    assert ring.cursor == second.id


@pytest.mark.asyncio
async def test_proxy_does_not_rotate_on_500_then_retries_same_key(
    ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.linkup import client as client_mod

    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(500, "boom"),
        FakeResponse(200, '{"ok":true}'),
    ]
    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: FakeClient(responses, calls))
    status, body, _headers = await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
    assert status == 200
    assert body == '{"ok":true}'
    assert [auth for _m, _u, auth in calls] == ["Bearer key-a", "Bearer key-a"]


@pytest.mark.asyncio
async def test_research_post_and_get_use_same_bound_key(ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import client as client_mod

    first = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(200, '{"id":"job-1","status":"pending"}'),
        FakeResponse(200, '{"id":"job-1","status":"completed"}'),
    ]
    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: FakeClient(responses, calls))
    status, body, _h = await proxy_linkup(method="POST", path="research", headers={}, content=b"{}")
    assert status == 200
    assert "job-1" in body
    bound = await key_for_job("job-1")
    assert bound.id == first.id

    status, _body, _h = await proxy_linkup(method="GET", path="research/job-1", headers={}, content=None)
    assert status == 200
    assert calls[1][2] == "Bearer key-a"


@pytest.mark.asyncio
async def test_get_after_bound_key_removed_fails_closed(ring: MemoryKeyDao) -> None:
    first = await add_key(label="a", api_key="key-a")
    await bind_job(upstream_job_id="job-9", key_id=first.id, kind="research")
    _ = await remove_key_safe(first.id)
    with pytest.raises(LinkupJobKeyRemovedError):
        _ = await key_for_job("job-9")
    with pytest.raises(LinkupProxyError) as exc:
        await proxy_linkup(method="GET", path="research/job-9", headers={}, content=None)
    assert exc.value.status_code == 410


async def remove_key_safe(key_id: UUID):
    from app.services.linkup.keys import remove_key

    return await remove_key(key_id)


@pytest.mark.asyncio
async def test_empty_ring_returns_503(ring: MemoryKeyDao) -> None:
    _ = ring
    with pytest.raises(LinkupProxyError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.status_code == 503


def test_path_allowlist_and_proxy_token() -> None:
    assert allowed_upstream_path("search") is True
    assert allowed_upstream_path("research/abc") is True
    assert allowed_upstream_path("admin") is False
    agent_id = uuid4()
    token = _proxy_token(agent_id, secret="s")
    assert parse_proxy_token(token, secret="s") == agent_id
    assert parse_proxy_token(token, secret="other") is None
    assert parse_proxy_token("not-a-token", secret="s") is None
    assert parse_proxy_token(f"{agent_id}.short", secret="s") is None


def _assert_ring_untouched(ring: MemoryKeyDao, spy: RingSpy, *keys: LinkupApiKeyRecord) -> None:
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
    limits.LINKUP_PROXY_MAX_RESPONSE_BYTES = 10
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    oversized = FakeResponse(200, chunks=[b"123456", b"789012", b"never-read"])
    healthy = FakeResponse(200, '{"ok":true}')
    calls = use_upstream(monkeypatch, [oversized, healthy])

    with pytest.raises(LinkupResponseTooLargeError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

    assert not isinstance(exc.value, LinkupProxyError)
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
    limits.LINKUP_PROXY_MAX_RESPONSE_BYTES = 10
    _ = await add_key(label="a", api_key="key-a")
    _ = use_upstream(monkeypatch, [FakeResponse(200, "x" * size)])
    if allowed:
        status, body, _headers = await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
        assert (status, body) == (200, "x" * size)
    else:
        with pytest.raises(LinkupResponseTooLargeError):
            await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")


@pytest.mark.asyncio
async def test_response_ceiling_counts_decoded_bytes(
    ring: MemoryKeyDao, limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = ring
    limits.LINKUP_PROXY_MAX_RESPONSE_BYTES = 50
    _ = await add_key(label="a", api_key="key-a")
    squeezed = gzip.compress(b"a" * 400)
    assert len(squeezed) < 50
    _ = use_upstream(monkeypatch, [FakeResponse(200, chunks=[squeezed], headers={"content-encoding": "gzip"})])
    with pytest.raises(LinkupResponseTooLargeError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
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

    with pytest.raises(LinkupDeadlineExceededError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

    assert not isinstance(exc.value, LinkupProxyError)
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
    calls = use_upstream(monkeypatch, [FakeResponse(402, '{"error":"payment required"}'), FakeResponse(200, "{}")])

    with pytest.raises(LinkupDeadlineExceededError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

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
    limits.LINKUP_PROXY_DEADLINE_SECONDS = 120.0
    first = await add_key(label="a", api_key="key-a")
    second = await add_key(label="b", api_key="key-b")
    calls = use_upstream(
        monkeypatch,
        [
            FakeResponse(500, "boom", on_send=lambda: clock.advance(100)),
            FakeResponse(200, '{"ok":true}', on_send=lambda: clock.advance(30)),
        ],
    )

    with pytest.raises(LinkupDeadlineExceededError):
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

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
    with pytest.raises(LinkupDeadlineExceededError):
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
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
    limits.LINKUP_API_KEY = "env-key"
    stored = await add_key(label="a", api_key="key-a")
    stored.status = "disabled"
    slow = FakeResponse(200, chunks=[b"abc", b"def"], after_chunk=lambda: clock.advance(31))
    calls = use_upstream(monkeypatch, [slow])

    with pytest.raises(LinkupDeadlineExceededError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

    assert [auth for _m, _u, auth in calls] == ["Bearer env-key"]
    assert slow.stream.closed
    assert spy.exhausted == []
    assert spy.advanced == []
    assert recorded[0]["key_id"] is None
    assert recorded[0]["local_failure"] is exc.value


@pytest.mark.asyncio
async def test_bound_poll_shares_the_bounds_and_is_not_recorded(
    ring: MemoryKeyDao,
    limits: SimpleNamespace,
    spy: RingSpy,
    clock: FakeClock,
    recorded: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    await bind_job(upstream_job_id="job-1", key_id=first.id, kind="research")
    limits.LINKUP_PROXY_MAX_RESPONSE_BYTES = 5
    _ = use_upstream(monkeypatch, [FakeResponse(200, "x" * 6)])
    with pytest.raises(LinkupResponseTooLargeError):
        await proxy_linkup(method="GET", path="research/job-1", headers={}, content=None)

    slow = FakeResponse(200, chunks=[b"ab", b"cd"], after_chunk=lambda: clock.advance(31))
    calls = use_upstream(monkeypatch, [slow])
    with pytest.raises(LinkupDeadlineExceededError):
        await proxy_linkup(method="GET", path="research/job-1", headers={}, content=None)

    assert [auth for _m, _u, auth in calls] == ["Bearer key-a"]
    assert recorded == []
    assert spy.exhausted == []
    assert spy.advanced == []


@pytest.mark.asyncio
async def test_redirects_are_still_followed(ring: MemoryKeyDao, monkeypatch: pytest.MonkeyPatch) -> None:
    _ = ring
    _ = await add_key(label="a", api_key="key-a")
    calls = use_upstream(
        monkeypatch,
        [
            FakeResponse(302, headers={"location": "https://api.linkup.so/v1/search/moved"}),
            FakeResponse(200, '{"ok":true}'),
        ],
    )
    status, body, _headers = await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
    assert (status, body) == (200, '{"ok":true}')
    assert [(method, url) for method, url, _auth in calls] == [
        ("POST", "https://api.linkup.so/v1/search"),
        ("GET", "https://api.linkup.so/v1/search/moved"),
    ]
    assert {auth for _m, _u, auth in calls} == {"Bearer key-a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 302, 307])
async def test_response_ceiling_applies_before_redirect_following_or_success(
    ring: MemoryKeyDao, limits: SimpleNamespace, spy: RingSpy, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    limits.LINKUP_PROXY_MAX_RESPONSE_BYTES = 10
    first = await add_key(label="a", api_key="key-a")
    oversized = FakeResponse(
        status, chunks=[b"123456", b"789012", b"not read"], headers={"location": "/v1/search/next"}
    )
    calls = use_upstream(monkeypatch, [oversized, FakeResponse(200, "{}")])

    with pytest.raises(LinkupResponseTooLargeError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

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
    url = "https://other.example/next" if status == 307 else "https://api.linkup.so/next"
    calls = use_upstream(
        monkeypatch, [FakeResponse(status, "redirect body", headers={"location": url}), FakeResponse(200, "{}")]
    )
    result = await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
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

    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: Refusing([], []))
    with pytest.raises(LinkupProxyError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")
    assert exc.value.status_code == 502


def _route_app(monkeypatch: pytest.MonkeyPatch, limits: SimpleNamespace, upstream: AsyncMock) -> FastAPI:
    agent_id = uuid4()
    monkeypatch.setattr(linkup_proxy, "parse_proxy_token", lambda _token: agent_id)
    monkeypatch.setattr(linkup_proxy, "agent_available", AsyncMock(return_value=True))
    monkeypatch.setattr(linkup_proxy, "get_settings", lambda: limits)
    monkeypatch.setattr(linkup_proxy, "proxy_linkup", upstream)
    app = FastAPI()
    app.include_router(linkup_proxy.router)

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
        monkeypatch.setattr(client_mod, "_httpx_client", lambda **_kwargs: httpx.AsyncClient(transport=transport))

    with pytest.raises(LinkupDeadlineExceededError) as exc:
        await proxy_linkup(method="POST", path="search", headers={}, content=b"{}")

    assert exc.value.bytes_read == (0 if stalled_at == "lookup" else 3)
    assert len(scopes) == 1
    assert closed == ([] if stalled_at == "lookup" else [True])
    assert [entry["local_failure"] for entry in recorded] == [exc.value]
    _assert_ring_untouched(ring, spy, first)


@pytest.mark.asyncio
async def test_route_rejects_chunked_body_over_the_ceiling_while_reading(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits.LINKUP_PROXY_MAX_REQUEST_BYTES = 1024
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    consumed: list[int] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy.test") as client:
        request = client.build_request(
            "POST",
            "/api/linkup/v1/search",
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
    limits.LINKUP_PROXY_MAX_REQUEST_BYTES = 1024
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    response = await _post(app, "/api/linkup/v1/search", _chunked(sizes, []))
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
    limits.LINKUP_PROXY_MAX_REQUEST_BYTES = 1024
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    response = await _post(app, "/api/linkup/v1/search", b"x" * size)
    assert response.status_code == status
    assert upstream.await_count == (1 if status == 200 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_path", "bad_token", "agent_unavailable"])
async def test_route_validates_path_and_token_before_reading_the_body(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    upstream = AsyncMock(return_value=(200, "{}", {}))
    app = _route_app(monkeypatch, limits, upstream)
    if failure == "bad_token":
        monkeypatch.setattr(linkup_proxy, "parse_proxy_token", lambda _token: None)
    if failure == "agent_unavailable":
        monkeypatch.setattr(linkup_proxy, "agent_available", AsyncMock(return_value=False))
    consumed: list[int] = []
    path = "/api/linkup/v1/admin" if failure == "unknown_path" else "/api/linkup/v1/search"
    response = await _post(app, path, _chunked([600] * 4, consumed))
    assert response.status_code == (404 if failure == "unknown_path" else 401)
    assert consumed == []
    upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_limit_does_not_cap_other_routes(limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    limits.LINKUP_PROXY_MAX_REQUEST_BYTES = 1024
    app = _route_app(monkeypatch, limits, AsyncMock(return_value=(200, "{}", {})))
    response = await _post(app, "/other", b"x" * 4096)
    assert response.status_code == 200
    assert response.json() == {"bytes": 4096}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status"),
    [(LinkupResponseTooLargeError(bytes_read=5), 502), (LinkupDeadlineExceededError(), 504)],
)
async def test_route_returns_the_local_failure_status(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: LinkupLimitError, status: int
) -> None:
    app = _route_app(monkeypatch, limits, AsyncMock(side_effect=failure))
    response = await _post(app, "/api/linkup/v1/search", b"{}")
    assert response.status_code == status
    assert response.json()["detail"]
