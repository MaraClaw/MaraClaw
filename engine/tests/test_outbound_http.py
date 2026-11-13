import asyncio
import json
from collections.abc import Callable
from typing import final
from uuid import uuid4

import httpx
import pytest

from app.records.outbound_plan import OutboundChunk, OutboundProvider
from app.services.channels import outbound_http, outbound_transport
from app.services.channels.outbound_errors import ProviderDeliveryError
from app.services.channels.outbound_http import outbound_client, reset_outbound_client, shutdown_outbound_client
from im_token_cache_fakes import install_token_cache
from test_channel_delivery_transport import configured, transport


@pytest.fixture
def token_cache(monkeypatch) -> dict[tuple[str, str, str], str]:
    return install_token_cache(monkeypatch)


@pytest.fixture(autouse=True)
def fresh_client():
    reset_outbound_client()
    yield
    reset_outbound_client()


def chunk() -> OutboundChunk:
    return OutboundChunk(text="hello", key=uuid4())


async def test_two_chunks_reuse_one_client_with_bounded_settings(monkeypatch):
    built: list[dict[str, object]] = []
    original = httpx.AsyncClient

    def factory(**kwargs):
        built.append(kwargs)
        return original(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True})), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    for _ in range(2):
        await outbound_transport.send_chunk(configured(OutboundProvider.SLACK), chunk())

    assert len(built) == 1
    assert built[0]["timeout"] == 20
    assert built[0]["follow_redirects"] is False
    limits = built[0]["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.max_connections is not None
    assert limits.max_keepalive_connections is not None


async def test_client_is_recreated_after_close_or_loop_change():
    first = outbound_client()
    assert outbound_client() is first

    await first.aclose()
    second = outbound_client()
    assert second is not first

    other_loop = asyncio.new_event_loop()
    try:
        outbound_http._loop = other_loop
        third = outbound_client()
    finally:
        other_loop.close()
    assert third is not second
    assert outbound_client() is third


async def test_shutdown_closes_client_and_next_use_builds_a_new_one():
    client = outbound_client()

    await shutdown_outbound_client()
    await shutdown_outbound_client()

    assert client.is_closed
    assert outbound_client() is not client


@final
class FeishuWire:
    def __init__(self, sends: list[httpx.Response]) -> None:
        self.sends: list[httpx.Response] = sends
        self.tokens: int = 0
        self.requests: list[httpx.Request] = []
        self.refuse_tokens: bool = False
        self.on_send: Callable[[], None] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if "app_access_token" in request.url.path:
            self.tokens += 1
            if self.refuse_tokens:
                return httpx.Response(200, json={"code": 10003, "msg": "invalid param"})
            return httpx.Response(200, json={"code": 0, "app_access_token": f"token-{self.tokens}", "expire": 7200})
        self.requests.append(request)
        if self.on_send is not None:
            self.on_send()
        return self.sends.pop(0) if len(self.sends) > 1 else self.sends[0]


def sent_ok() -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "data": {"message_id": "om_1"}})


async def test_feishu_issues_one_token_request_for_two_chunks_under_service_cache_key(monkeypatch, token_cache):
    wire = FeishuWire([sent_ok()])
    transport(monkeypatch, wire)

    for _ in range(2):
        await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())

    assert wire.tokens == 1
    assert [request.headers["Authorization"] for request in wire.requests] == ["Bearer token-1"] * 2
    assert token_cache == {("feishu", "application", "live-secret"): "token-1"}


@pytest.mark.parametrize(
    "rejection",
    [
        httpx.Response(400, json={"code": 99991663, "msg": "Invalid access token"}),
        httpx.Response(200, json={"code": 99991668, "msg": "Invalid access token"}),
    ],
)
async def test_feishu_auth_failure_drops_cached_token_and_retries_once_with_same_uuid(
    monkeypatch, token_cache, rejection
):
    token_cache[("feishu", "application", "live-secret")] = "stale"
    wire = FeishuWire([rejection, sent_ok()])
    transport(monkeypatch, wire)

    await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())

    assert wire.tokens == 1
    assert [request.headers["Authorization"] for request in wire.requests] == ["Bearer stale", "Bearer token-1"]
    bodies = [json.loads(request.content) for request in wire.requests]
    assert bodies[0] == bodies[1]
    assert token_cache == {("feishu", "application", "live-secret"): "token-1"}


async def test_feishu_persistent_auth_failure_retries_only_once(monkeypatch, token_cache):
    rejection = httpx.Response(400, json={"code": 99991663, "msg": "Invalid access token"})
    wire = FeishuWire([rejection])
    transport(monkeypatch, wire)

    with pytest.raises(httpx.HTTPStatusError):
        await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())

    assert len(wire.requests) == 2
    assert wire.tokens == 2


async def test_feishu_other_failure_is_not_retried_and_keeps_cached_token(monkeypatch, token_cache):
    wire = FeishuWire([httpx.Response(200, json={"code": 230001, "msg": "bad receive_id"})])
    transport(monkeypatch, wire)

    with pytest.raises(ProviderDeliveryError) as failure:
        await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())

    assert failure.value.code == "230001"
    assert len(wire.requests) == 1
    assert wire.tokens == 1
    assert token_cache == {("feishu", "application", "live-secret"): "token-1"}


async def test_concurrent_chunks_share_one_feishu_token_request(monkeypatch, token_cache):
    wire = FeishuWire([sent_ok()])
    transport(monkeypatch, wire)

    await asyncio.gather(
        *(outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk()) for _ in range(4))
    )

    assert wire.tokens == 1
    assert [request.headers["Authorization"] for request in wire.requests] == ["Bearer token-1"] * 4


async def test_refused_feishu_token_raises_typed_error_and_is_not_cached(monkeypatch, token_cache):
    wire = FeishuWire([sent_ok()])
    wire.refuse_tokens = True
    transport(monkeypatch, wire)

    with pytest.raises(ProviderDeliveryError) as failure:
        await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())

    assert failure.value.code == "authentication_failed"
    assert token_cache == {}
    wire.refuse_tokens = False
    await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())
    assert wire.tokens == 2
    assert token_cache == {("feishu", "application", "live-secret"): "token-2"}


async def test_feishu_rejection_keeps_a_token_a_peer_stored_meanwhile(monkeypatch, token_cache):
    key = ("feishu", "application", "live-secret")
    token_cache[key] = "stale"
    rejection = httpx.Response(400, json={"code": 99991663, "msg": "Invalid access token"})
    wire = FeishuWire([rejection, sent_ok()])
    wire.on_send = lambda: token_cache.update({key: "peer"}) if len(wire.requests) == 1 else None
    transport(monkeypatch, wire)

    await outbound_transport.send_chunk(configured(OutboundProvider.FEISHU), chunk())

    assert wire.tokens == 0
    assert [request.headers["Authorization"] for request in wire.requests] == ["Bearer stale", "Bearer peer"]
    assert token_cache == {key: "peer"}
