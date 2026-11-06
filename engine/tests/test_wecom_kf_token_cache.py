import base64
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api import wecom
from app.core.domain.channels import ChannelType
from app.records.channel_config import ChannelConfigRecord
from im_token_cache_fakes import install_token_cache


class WeComWire:
    def __init__(self, rejections: list[int]) -> None:
        self.rejections = rejections
        self.token_requests = 0
        self.sync_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gettoken"):
            self.token_requests += 1
            return httpx.Response(200, json={"errcode": 0, "access_token": "fresh", "expires_in": 3600})
        self.sync_requests.append(request)
        if self.rejections:
            return httpx.Response(200, json={"errcode": self.rejections.pop(0)})
        return httpx.Response(200, json={"errcode": 0, "has_more": 0, "msg_list": []})


@pytest_asyncio.fixture
async def kf_callback(monkeypatch):
    agent_id = uuid4()
    key = base64.b64encode(b"a" * 32).decode().rstrip("=")
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent_id,
        channel_type=ChannelType.WECOM,
        app_id="corp",
        app_secret="secret",
        verification_token="verify",
        encrypt_key=key,
        is_configured=True,
        is_connected=False,
        extra_config={},
        created_at=None,
    )
    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    cache = install_token_cache(monkeypatch)
    app = FastAPI()
    app.include_router(wecom.router)
    original_client = httpx.AsyncClient
    wire = WeComWire([])
    client = original_client(transport=httpx.ASGITransport(app=app), base_url="http://test")
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(wire), **kwargs)
    )
    encrypted = wecom._encrypt_msg(
        key, "<xml><MsgType>event</MsgType><Event>kf_msg_or_event</Event><Token>ENCcursor</Token></xml>", "corp"
    )
    signature = wecom._verify_signature("verify", "1", "2", encrypted)

    async def callback():
        return await client.post(
            f"/channel/wecom/{agent_id}/webhook",
            params={"msg_signature": signature, "timestamp": "1", "nonce": "2"},
            content=f"<xml><Encrypt>{encrypted}</Encrypt></xml>",
        )

    try:
        yield callback, wire, cache
    finally:
        await client.aclose()


async def test_two_kf_callbacks_reuse_cached_wecom_token(kf_callback):
    callback, wire, cache = kf_callback
    first = await callback()
    second = await callback()
    assert (first.status_code, second.status_code) == (200, 200)
    assert wire.token_requests == 1
    assert [request.url.params["access_token"] for request in wire.sync_requests] == ["fresh", "fresh"]
    assert cache == {("wecom", "corp", "secret"): "fresh"}


@pytest.mark.parametrize("errcode", [40014, 42001, 40001])
async def test_kf_invalid_token_is_dropped_and_sync_retried_once(kf_callback, errcode):
    callback, wire, cache = kf_callback
    cache[("wecom", "corp", "secret")] = "stale"
    wire.rejections.append(errcode)
    response = await callback()
    assert response.status_code == 200
    assert wire.token_requests == 1
    assert [request.url.params["access_token"] for request in wire.sync_requests] == ["stale", "fresh"]
    assert json.loads(wire.sync_requests[0].content) == json.loads(wire.sync_requests[1].content)
    assert cache == {("wecom", "corp", "secret"): "fresh"}


async def test_kf_repeated_invalid_token_stops_after_one_retry(kf_callback):
    callback, wire, cache = kf_callback
    cache[("wecom", "corp", "secret")] = "stale"
    wire.rejections.extend([40014, 40014])
    response = await callback()
    assert response.status_code == 503
    assert wire.token_requests == 1
    assert len(wire.sync_requests) == 2
    assert cache == {}
