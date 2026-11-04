"""Capture billed Linkup calls as web_search_events (no live PG/Linkup)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from tests.test_linkup_proxy_client import (
    FakeClock,
    FakeResponse,
    MemoryJobDao,
    MemoryKeyDao,
    proxy_settings,
    use_upstream,
)

from app.records.web_search_event import WebSearchEventRecord
from app.services.linkup.analytics import (
    cheap_result_count,
    hash_text,
    host_only,
    is_billed_call,
    normalize_text,
    record_linkup_call,
    status_class_for,
)
from app.services.linkup.export import event_to_export_row, object_key


class MemoryEventDao:
    def __init__(self) -> None:
        self.rows: list[WebSearchEventRecord] = []
        self.payloads: dict[UUID, str] = {}

    async def create(self, *, obj_in: dict[str, Any]) -> WebSearchEventRecord:
        record = WebSearchEventRecord.from_row(
            {
                "id": obj_in.get("id") or uuid4(),
                **obj_in,
            }
        )
        self.rows.append(record)
        return record

    async def insert_payload(self, *, event_id: UUID, raw_query: str) -> None:
        self.payloads[event_id] = raw_query


class MemoryAgentDao:
    def __init__(self, tenant_id: UUID | None) -> None:
        self.tenant_id = tenant_id

    async def get(self, agent_id: UUID):
        del agent_id
        return SimpleNamespace(tenant_id=self.tenant_id)


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED": True,
        "WEB_SEARCH_ANALYTICS_EXPORT_ENABLED": False,
        "WEB_SEARCH_ANALYTICS_INCLUDE_RAW": False,
        "WEB_SEARCH_ANALYTICS_HASH_KEY": "analytics-secret",
        "SECRET_KEY": "app-secret",
        "ANALYTICS_S3_BUCKET": "",
        "S3_BUCKET": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_billed_vs_poll() -> None:
    assert is_billed_call("POST", "search") is True
    assert is_billed_call("POST", "fetch") is True
    assert is_billed_call("POST", "research") is True
    assert is_billed_call("GET", "research/job-1") is False
    assert is_billed_call("GET", "search") is False


def test_normalize_and_host() -> None:
    assert normalize_text("  Who   IS the CEO of Stripe? ") == "who is the ceo of stripe?"
    assert host_only("https://docs.stripe.com/api?key=secret") == "docs.stripe.com"
    assert host_only("docs.stripe.com/path") == "docs.stripe.com"


def test_status_class_and_result_count() -> None:
    assert status_class_for(200, "{}") == "ok"
    assert status_class_for(429, '{"error":"insufficient quota"}') == "quota"
    assert status_class_for(400, "bad") == "client_error"
    assert status_class_for(502, "nope") == "upstream_error"
    assert cheap_result_count('{"results":[1,2,3]}') == 3
    assert cheap_result_count("not-json") is None


def test_object_key_never_uses_agents_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from app.services.linkup import export as export_mod

    monkeypatch.setattr(
        export_mod,
        "get_settings",
        lambda: SimpleNamespace(ANALYTICS_S3_PREFIX="agents"),
    )
    key = object_key(now=datetime(2026, 8, 15, 14, 3, tzinfo=UTC), instance_id="box 1", object_id="abc")
    assert key.startswith("web-search/dt=2026-08-15/hour=14/")
    assert "agents/" not in key
    assert key.endswith(".jsonl.gz")


@pytest.mark.asyncio
async def test_record_search_omits_raw_and_sets_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.linkup import analytics as analytics_mod

    store = MemoryEventDao()
    tenant = uuid4()
    agent = uuid4()
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", store)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(tenant))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())

    await record_linkup_call(
        agent_id=agent,
        method="POST",
        path="search",
        content=b'{"q":"Who is the CEO of Stripe?"}',
        status=200,
        body='{"results":[{"url":"https://stripe.com"}]}',
        latency_ms=12,
        key_id=uuid4(),
    )
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row.tenant_id == tenant
    assert row.agent_id == agent
    assert row.kind == "search"
    assert row.query_normalized == "who is the ceo of stripe?"
    assert "stripe" in row.query_normalized
    assert row.query_char_len == len("Who is the CEO of Stripe?")
    assert row.export_state == "skipped"
    assert store.payloads == {}
    dumped = event_to_export_row(row, raw_query=None)
    assert "query_raw" in dumped
    assert dumped["query_raw"] is None
    assert "Who is the CEO" not in str(row)


@pytest.mark.asyncio
async def test_record_fetch_stores_host_not_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import analytics as analytics_mod

    store = MemoryEventDao()
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", store)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(None))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())

    await record_linkup_call(
        agent_id=uuid4(),
        method="POST",
        path="fetch",
        content=b'{"url":"https://internal.example.com/secret?token=abc"}',
        status=200,
        body="{}",
        latency_ms=8,
        key_id=None,
    )
    row = store.rows[0]
    assert row.primary_domain == "internal.example.com"
    assert row.query_normalized == "internal.example.com"
    assert "token=abc" not in row.query_normalized
    assert "secret" not in row.query_normalized


@pytest.mark.asyncio
async def test_record_skips_polls_and_disabled_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import analytics as analytics_mod

    store = MemoryEventDao()
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", store)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(None))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())

    await record_linkup_call(
        agent_id=uuid4(),
        method="GET",
        path="research/job-1",
        content=None,
        status=200,
        body="{}",
        latency_ms=3,
        key_id=None,
    )
    assert store.rows == []

    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings(WEB_SEARCH_ANALYTICS_CAPTURE_ENABLED=False))
    await record_linkup_call(
        agent_id=uuid4(),
        method="POST",
        path="search",
        content=b'{"q":"x"}',
        status=200,
        body="{}",
        latency_ms=1,
        key_id=None,
    )
    assert store.rows == []


@pytest.mark.asyncio
async def test_record_insert_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import analytics as analytics_mod

    class BoomDao:
        async def create(self, *, obj_in: dict[str, Any]) -> None:
            del obj_in
            raise RuntimeError("db down")

    monkeypatch.setattr(analytics_mod, "web_search_event_dao", BoomDao())
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(None))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())
    await record_linkup_call(
        agent_id=uuid4(),
        method="POST",
        path="search",
        content=b'{"q":"x"}',
        status=200,
        body="{}",
        latency_ms=1,
        key_id=None,
    )


@pytest.mark.asyncio
async def test_include_raw_writes_side_table_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import analytics as analytics_mod

    store = MemoryEventDao()
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", store)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(None))
    monkeypatch.setattr(
        analytics_mod,
        "get_settings",
        lambda: _settings(
            WEB_SEARCH_ANALYTICS_EXPORT_ENABLED=True,
            WEB_SEARCH_ANALYTICS_INCLUDE_RAW=True,
            ANALYTICS_S3_BUCKET="lake",
        ),
    )
    await record_linkup_call(
        agent_id=uuid4(),
        method="POST",
        path="search",
        content=b'{"query":"Exact Wording"}',
        status=200,
        body="{}",
        latency_ms=4,
        key_id=None,
    )
    row = store.rows[0]
    assert row.export_state == "pending"
    assert "Exact Wording" not in row.query_normalized
    assert store.payloads[row.id] == "Exact Wording"


@pytest.mark.asyncio
async def test_include_raw_without_export_does_not_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import analytics as analytics_mod

    store = MemoryEventDao()
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", store)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(None))
    monkeypatch.setattr(
        analytics_mod,
        "get_settings",
        lambda: _settings(WEB_SEARCH_ANALYTICS_INCLUDE_RAW=True, WEB_SEARCH_ANALYTICS_EXPORT_ENABLED=False),
    )
    await record_linkup_call(
        agent_id=uuid4(),
        method="POST",
        path="search",
        content=b'{"query":"secret raw"}',
        status=200,
        body="{}",
        latency_ms=1,
        key_id=None,
    )
    assert store.rows[0].export_state == "skipped"
    assert store.payloads == {}


def test_hmac_hash_uses_dedicated_secret() -> None:
    assert hash_text("hello", secret="analytics-secret") != hash_text("hello", secret="other")
    assert len(hash_text("hello", secret="analytics-secret")) == 64


def test_float_from_row_accepts_decimal() -> None:
    from decimal import Decimal

    from app.core.json_types import float_from_row

    assert float_from_row(Decimal("12.7")) == 12.7


@pytest.mark.asyncio
async def test_proxy_post_records_agent_and_skips_get_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_linkup_proxy_client import FakeClient, FakeResponse, MemoryJobDao, MemoryKeyDao, proxy_settings

    from app.services.linkup import analytics as analytics_mod, client as client_mod, jobs as jobs_mod, keys as keys_mod
    from app.services.linkup.client import proxy_linkup
    from app.services.linkup.keys import add_key

    keys = MemoryKeyDao()
    jobs = MemoryJobDao()
    events = MemoryEventDao()
    agent = uuid4()
    settings = proxy_settings()
    monkeypatch.setattr(keys_mod, "linkup_api_key_dao", keys)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs_mod, "linkup_api_key_dao", keys)
    monkeypatch.setattr(jobs_mod, "linkup_async_job_dao", jobs)
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", events)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(uuid4()))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())

    _ = await add_key(label="a", api_key="key-a")
    calls: list[tuple[str, str, str]] = []
    responses = [
        FakeResponse(200, '{"results":[1]}'),
        FakeResponse(200, '{"id":"job-1","status":"pending"}'),
        FakeResponse(200, '{"id":"job-1","status":"completed"}'),
    ]
    monkeypatch.setattr(client_mod, "_httpx_client", lambda *a, **k: FakeClient(responses, calls))

    status, _body, _h = await proxy_linkup(
        method="POST",
        path="search",
        headers={},
        content=b'{"q":"Stripe CEO"}',
        agent_id=agent,
    )
    assert status == 200
    assert len(events.rows) == 1
    assert events.rows[0].agent_id == agent
    assert events.rows[0].kind == "search"

    status, _body, _h = await proxy_linkup(
        method="POST",
        path="research",
        headers={},
        content=b'{"q":"deep"}',
        agent_id=agent,
    )
    assert status == 200
    assert len(events.rows) == 2

    status, _body, _h = await proxy_linkup(
        method="GET",
        path="research/job-1",
        headers={},
        content=None,
        agent_id=agent,
    )
    assert status == 200
    assert len(events.rows) == 2


@pytest.mark.asyncio
async def test_quota_rotated_marks_error_class(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.linkup import analytics as analytics_mod

    store = MemoryEventDao()
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", store)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(None))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())
    await record_linkup_call(
        agent_id=uuid4(),
        method="POST",
        path="search",
        content=b'{"q":"x"}',
        status=200,
        body="{}",
        latency_ms=3,
        key_id=None,
        quota_rotated=True,
    )
    assert store.rows[0].status_class == "ok"
    assert store.rows[0].error_class == "quota_rotated"


@pytest.mark.asyncio
async def test_proxy_v1_rejects_bad_token() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import HTTPException

    from app.api.linkup_proxy import proxy_v1

    request = MagicMock()
    request.body = AsyncMock(return_value=b"{}")
    with pytest.raises(HTTPException) as exc:
        await proxy_v1("search", request, authorization="Bearer not-a-token")
    assert exc.value.status_code == 401


@pytest.fixture
def bounded_proxy(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from app.api import linkup_proxy
    from app.services.linkup import analytics as analytics_mod, client as client_mod, jobs as jobs_mod, keys as keys_mod

    keys = MemoryKeyDao()
    events = MemoryEventDao()
    clock = FakeClock()
    settings = proxy_settings(LINKUP_PROXY_MAX_RESPONSE_BYTES=10)
    agent = uuid4()
    monkeypatch.setattr(keys_mod, "linkup_api_key_dao", keys)
    monkeypatch.setattr(keys_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(jobs_mod, "linkup_api_key_dao", keys)
    monkeypatch.setattr(jobs_mod, "linkup_async_job_dao", MemoryJobDao())
    monkeypatch.setattr(client_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(client_mod, "_monotonic", clock)
    monkeypatch.setattr(analytics_mod, "web_search_event_dao", events)
    monkeypatch.setattr(analytics_mod, "agent_dao", MemoryAgentDao(uuid4()))
    monkeypatch.setattr(analytics_mod, "get_settings", lambda: _settings())
    monkeypatch.setattr(linkup_proxy, "get_settings", lambda: settings)
    monkeypatch.setattr(linkup_proxy, "parse_proxy_token", lambda _token: agent)
    monkeypatch.setattr(linkup_proxy, "agent_available", AsyncMock(return_value=True))
    app = FastAPI()
    app.include_router(linkup_proxy.router)
    return SimpleNamespace(keys=keys, events=events, clock=clock, settings=settings, app=app, agent=agent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "error_class", "response_bytes", "attempts"),
    [
        ("ceiling", 502, "response_ceiling", 12, 1),
        ("deadline", 504, "deadline_exceeded", 4, 1),
        ("rotation", 504, "deadline_exceeded", 0, 1),
        ("upstream", 500, "upstream_error", 4, 2),
    ],
)
async def test_bounded_post_records_one_event_at_http_boundary(
    bounded_proxy: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    status: int,
    error_class: str,
    response_bytes: int,
    attempts: int,
) -> None:
    from app.services.linkup import client as client_mod
    from app.services.linkup.keys import add_key

    first = await add_key(label="a", api_key="key-a")
    _ = await add_key(label="b", api_key="key-b")
    responses = [FakeResponse(500, "boom"), FakeResponse(500, "boom")]
    if failure == "ceiling":
        responses = [FakeResponse(200, chunks=[b"123456", b"789012", b"not read"])]
    if failure == "deadline":
        responses = [FakeResponse(500, "boom", on_send=lambda: bounded_proxy.clock.advance(31))]
    if failure == "rotation":
        responses = [FakeResponse(402, "quota")]
        advance = client_mod.advance_cursor

        async def expire_during_rotation(key_id: UUID):
            result = await advance(key_id)
            bounded_proxy.clock.advance(31)
            return result

        monkeypatch.setattr(client_mod, "advance_cursor", expire_during_rotation)
    calls = use_upstream(monkeypatch, responses)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bounded_proxy.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/linkup/v1/search", content=b'{"q":"x"}', headers={"Authorization": "Bearer agent.digest"}
        )

    assert response.status_code == status
    assert len(calls) == attempts
    assert len(bounded_proxy.events.rows) == 1
    row = bounded_proxy.events.rows[0]
    assert (row.http_status, row.error_class, row.response_bytes) == (status, error_class, response_bytes)
    assert row.key_id == first.id
    assert row.agent_id == bounded_proxy.agent
    assert len(row.error_class) <= 40


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [False, True])
async def test_oversize_request_has_no_key_lookup_upstream_or_event(
    bounded_proxy: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, declared_length: bool
) -> None:
    from tests.test_linkup_proxy_client import _chunked

    from app.services.linkup import client as client_mod

    lookup = AsyncMock()
    monkeypatch.setattr(client_mod, "ensure_env_key_seeded", lookup)
    calls = use_upstream(monkeypatch, [])
    consumed: list[int] = []
    headers = {"Authorization": "Bearer agent.digest"}
    if declared_length:
        headers["Content-Length"] = "1025"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bounded_proxy.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/linkup/v1/search", content=_chunked([512, 513, 1024], consumed), headers=headers
        )

    assert response.status_code == 413
    assert consumed == ([] if declared_length else [512, 513])
    lookup.assert_not_awaited()
    assert calls == []
    assert bounded_proxy.events.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ceiling", "deadline"])
async def test_failed_bound_poll_has_no_event(
    bounded_proxy: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from app.services.linkup.jobs import bind_job
    from app.services.linkup.keys import add_key

    key = await add_key(label="a", api_key="key-a")
    await bind_job(upstream_job_id="job-1", key_id=key.id, kind="research")
    upstream = FakeResponse(200, "x" * 11)
    status = 502
    if failure == "deadline":
        upstream = FakeResponse(200, "ok", on_send=lambda: bounded_proxy.clock.advance(31))
        status = 504
    calls = use_upstream(monkeypatch, [upstream])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bounded_proxy.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/linkup/v1/research/job-1", headers={"Authorization": "Bearer agent.digest"})

    assert response.status_code == status
    assert len(calls) == 1
    assert upstream.stream.closed
    assert bounded_proxy.events.rows == []


@pytest.mark.asyncio
async def test_analytics_write_is_outside_deadline(
    bounded_proxy: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from app.services.linkup import client as client_mod
    from app.services.linkup.keys import add_key

    _ = await add_key(label="a", api_key="key-a")
    _ = use_upstream(monkeypatch, [FakeResponse(200, "{}")])
    real_timeout = asyncio.timeout
    scopes: list[asyncio.Timeout] = []

    def capture_timeout(delay: float | None) -> asyncio.Timeout:
        scope = real_timeout(delay)
        scopes.append(scope)
        return scope

    create = bounded_proxy.events.create

    async def create_after_budget(*, obj_in: dict[str, Any]):
        bounded_proxy.clock.advance(31)
        with pytest.raises(RuntimeError, match="finished Timeout"):
            scopes[0].reschedule(asyncio.get_running_loop().time())
        return await create(obj_in=obj_in)

    monkeypatch.setattr(client_mod.asyncio, "timeout", capture_timeout)
    monkeypatch.setattr(bounded_proxy.events, "create", create_after_budget)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bounded_proxy.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/linkup/v1/search", content=b'{"q":"x"}', headers={"Authorization": "Bearer agent.digest"}
        )

    assert response.status_code == 200
    assert len(bounded_proxy.events.rows) == 1
    assert bounded_proxy.events.rows[0].error_class is None
