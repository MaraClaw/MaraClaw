import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from app.records.outbound_plan import OutboundChunk, OutboundProvider
from app.services import im_token_cache
from app.services.channels.outbound_errors import ProviderDeliveryError, ProviderPayloadRejectedError
from app.services.channels.outbound_transport import send_chunk
from app.services.wechat_channel import WECHAT_CONTEXT_CACHE_KEY
from test_channel_delivery_transport import configured, transport


@pytest.mark.parametrize("provider", ["microsoft_teams", "wecom", "dingtalk", "wechat"])
@pytest.mark.parametrize("accepted", [True, False])
async def test_enterprise_transport_sends_one_exact_chunk_and_requires_ack(
    monkeypatch, provider: OutboundProvider, accepted
):
    requests: list[httpx.Request] = []
    outbound = configured(provider)
    outbound.config.extra_config.update(
        {
            "bot_token": "live-bot-token",
            WECHAT_CONTEXT_CACHE_KEY: {
                "destination": {"context_token": "live-context"},
            },
        }
    )
    outbound = replace(
        outbound, target=outbound.target.model_copy(update={"endpoint": "https://smba.trafficmanager.net/amer"})
    )
    chunk = OutboundChunk(text="  exact\n世界  ", key=uuid4())

    async def no_cache(*_args, **_kwargs):
        return None

    def respond(request: httpx.Request):
        requests.append(request)
        if "oauth2" in request.url.path or "gettoken" in request.url.path:
            return httpx.Response(200, json={"errcode": 0, "access_token": "fresh-token", "expires_in": 3600})
        return httpx.Response(
            200, json={"id": "ack", "errcode": 0, "ret": 0, "processQueryKey": "ack"} if accepted else {}
        )

    monkeypatch.setattr(im_token_cache, "get_cached_im_token", no_cache)
    monkeypatch.setattr(im_token_cache, "set_cached_im_token", no_cache)
    transport(monkeypatch, respond)

    if accepted:
        await send_chunk(outbound, chunk)
    else:
        with pytest.raises(ProviderDeliveryError) as error:
            await send_chunk(outbound, chunk)
        assert not isinstance(error.value, ProviderPayloadRejectedError)

    messages = [
        request for request in requests if "oauth2" not in request.url.path and "gettoken" not in request.url.path
    ]
    assert len(messages) == 1
    body = json.loads(messages[0].content)
    match provider:
        case "microsoft_teams":
            assert body["text"] == chunk.text
        case "wecom":
            assert body["text"]["content"] == chunk.text
        case "dingtalk":
            assert json.loads(body["msgParam"])["content"] == chunk.text
        case "wechat":
            assert body["msg"]["item_list"][0]["text_item"]["text"] == chunk.text
            assert body["msg"]["context_token"] == "live-context"
            assert messages[0].headers["Authorization"] == "Bearer live-secret"


async def test_teams_token_cache_is_bound_to_current_credentials(monkeypatch):
    outbound = configured("microsoft_teams")
    outbound = replace(
        outbound, target=outbound.target.model_copy(update={"endpoint": "https://smba.trafficmanager.net/amer"})
    )
    credentials: list[bytes] = []
    authorizations: list[str] = []

    def respond(request: httpx.Request):
        if "oauth2" in request.url.path:
            credentials.append(request.content)
            return httpx.Response(200, json={"access_token": f"token-{len(credentials)}", "expires_in": 3600})
        authorizations.append(request.headers["Authorization"])
        return httpx.Response(200, json={"id": "ack"})

    transport(monkeypatch, respond)

    await send_chunk(outbound, OutboundChunk(text="first", key=uuid4()))
    outbound.config.app_secret = "rotated-secret"
    await send_chunk(outbound, OutboundChunk(text="second", key=uuid4()))

    assert len(credentials) == 2
    assert b"client_secret=live-secret" in credentials[0]
    assert b"client_secret=rotated-secret" in credentials[1]
    assert authorizations == ["Bearer token-1", "Bearer token-2"]
