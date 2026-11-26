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
from im_token_cache_fakes import TeamsWire, TokenCache, install_token_cache
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


@pytest.fixture
def token_cache(monkeypatch: pytest.MonkeyPatch) -> TokenCache:
    return install_token_cache(monkeypatch)


def _teams_outbound():
    outbound = configured(OutboundProvider.MICROSOFT_TEAMS)
    return replace(
        outbound, target=outbound.target.model_copy(update={"endpoint": "https://smba.trafficmanager.net/amer"})
    )


async def test_teams_token_cache_is_bound_to_current_credentials(monkeypatch, token_cache):
    outbound = _teams_outbound()
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


def _rejected() -> httpx.Response:
    return httpx.Response(401, json={"error": {"code": "Unauthorized"}})


async def test_teams_rejected_token_is_dropped_and_the_chunk_retried_once_with_a_fresh_one(monkeypatch, token_cache):
    wire = TeamsWire()
    wire.replies = [_rejected(), httpx.Response(200, json={"id": "ack"})]
    transport(monkeypatch, wire)

    await send_chunk(_teams_outbound(), OutboundChunk(text="report", key=uuid4()))

    assert wire.authorizations == ["Bearer token-1", "Bearer token-2"]
    assert len(wire.logins) == 2
    ((key, token),) = token_cache.items()
    assert token == "token-2"
    assert token_cache.rejected == [(key, "token-1")]
    bodies = [json.loads(request.content) for request in wire.activities]
    assert bodies[0] == bodies[1]


async def test_teams_second_rejection_is_reported_after_exactly_one_retry(monkeypatch, token_cache):
    wire = TeamsWire()
    wire.replies = [_rejected()]
    transport(monkeypatch, wire)

    with pytest.raises(httpx.HTTPStatusError):
        await send_chunk(_teams_outbound(), OutboundChunk(text="report", key=uuid4()))

    assert wire.authorizations == ["Bearer token-1", "Bearer token-2"]


async def test_teams_other_failures_keep_the_cached_token_and_are_not_retried(monkeypatch, token_cache):
    wire = TeamsWire()
    wire.replies = [httpx.Response(403, json={"error": {"code": "BotNotInConversationRoster"}})]
    transport(monkeypatch, wire)

    with pytest.raises(httpx.HTTPStatusError):
        await send_chunk(_teams_outbound(), OutboundChunk(text="report", key=uuid4()))

    assert wire.authorizations == ["Bearer token-1"]
    assert token_cache.rejected == []
    assert list(token_cache.values()) == ["token-1"]


async def test_teams_token_is_shared_by_every_configuration_of_the_same_bot(monkeypatch, token_cache):
    wire = TeamsWire()
    transport(monkeypatch, wire)
    first, second = _teams_outbound(), _teams_outbound()
    assert first.config.id != second.config.id
    assert first.config.agent_id != second.config.agent_id

    for outbound in (first, second):
        await send_chunk(outbound, OutboundChunk(text="report", key=uuid4()))

    assert len(wire.logins) == 1
    assert wire.authorizations == ["Bearer token-1"] * 2
