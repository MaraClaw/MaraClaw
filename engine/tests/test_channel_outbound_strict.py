import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from app.core.json_types import JsonObject
from app.records.agent import AgentRecord
from app.records.channel_config import ChannelConfigRecord
from app.records.chat import ChatSessionRecord
from app.services.channels import outbound

CHANNELS = ("feishu", "slack", "microsoft_teams", "google_chat", "wecom", "dingtalk", "whatsapp", "wechat", "discord")


def _session(channel: str, external: str | None = "recipient") -> tuple[AgentRecord, ChatSessionRecord]:
    agent = AgentRecord(id=uuid4(), creator_id=uuid4(), name="Bot")
    if channel == "google_chat" and external == "recipient":
        external = "google_chat_spaces/space"
    return agent, ChatSessionRecord(
        id=uuid4(), agent_id=agent.id, user_id=uuid4(), source_channel=channel, external_conv_id=external
    )


@pytest.mark.parametrize("channel", CHANNELS)
async def test_missing_config_is_retry_visible(monkeypatch: pytest.MonkeyPatch, channel: str) -> None:
    agent, session = _session(channel)

    async def missing(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(outbound.channel_config_dao, "get_configured_for_agent", missing)
    with pytest.raises(ValueError):
        await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)


@pytest.mark.parametrize(
    ("channel", "external"),
    [
        ("slack", None),
        ("slack", "slack_dm_"),
        ("unknown", "id"),
        ("wecom", "wecom_group_id"),
        ("dingtalk", "dingtalk_group_id"),
    ],
)
async def test_unsupported_destination_is_retry_visible(channel: str, external: str | None) -> None:
    agent, session = _session(channel, external)
    with pytest.raises(ValueError):
        await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> ChannelConfigRecord:
    from app.services.wechat_channel import update_wechat_context_cache

    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=uuid4(),
        app_id="app",
        app_secret="secret",
        is_configured=True,
        extra_config=update_wechat_context_cache(
            {
                "wecom_agent_id": "1",
                "service_url": "https://teams.example",
                "bot_token": "token",
                "service_account_json": {"client_email": "bot@example.test", "private_key": "fake"},
            },
            from_user_id="recipient",
            context_token="context",
            conv_id="recipient",
        ),
    )

    async def lookup(*args: object, **kwargs: object) -> ChannelConfigRecord:
        return config

    monkeypatch.setattr(outbound.channel_config_dao, "get_configured_for_agent", lookup)
    return config


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Callable[[int, JsonObject], list[httpx.Request]]:
    original = httpx.AsyncClient

    def install(status: int, body: JsonObject) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if "gettoken" in request.url.path:
                return httpx.Response(200, json={"errcode": 0, "access_token": "token"})
            if "app_access_token" in request.url.path:
                return httpx.Response(200, json={"code": 0, "app_access_token": "token"})
            return httpx.Response(status, json=body)

        def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return original(transport=httpx.MockTransport(handle))

        monkeypatch.setattr(httpx, "AsyncClient", client)
        return requests

    return install


@pytest.fixture(autouse=True)
def token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import teams
    from app.services import im_token_cache
    from app.services.channels import google_chat

    async def no_cache(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(im_token_cache, "get_cached_im_token", no_cache)
    monkeypatch.setattr(im_token_cache, "set_cached_im_token", no_cache)
    monkeypatch.setattr(im_token_cache, "drop_cached_im_token", no_cache)

    async def access_token(*args: object, **kwargs: object) -> str:
        return "token"

    monkeypatch.setattr(teams, "_get_teams_access_token", access_token)
    monkeypatch.setattr(google_chat, "_service_account_access_token", access_token)


@pytest.mark.parametrize(
    ("channel", "body"),
    [
        ("slack", {"ok": False, "error": "channel_not_found"}),
        ("wecom", {"errcode": 40003}),
        ("wecom", {"errcode": 0, "invaliduser": "recipient"}),
        ("dingtalk", {"code": "Forbidden"}),
        ("dingtalk", {"processQueryKey": "query", "invalidStaffIdList": ["recipient"]}),
        ("dingtalk", {"processQueryKey": "query", "flowControlledStaffIdList": ["recipient"]}),
        ("feishu", {"code": 999}),
        ("whatsapp", {"error": {"code": 100}}),
        ("discord", {}),
        ("microsoft_teams", {}),
        ("google_chat", {}),
        ("wechat", {}),
        ("wechat", {"ret": 1}),
    ],
)
async def test_provider_rejection_is_retry_visible(configured, wire, channel, body) -> None:
    wire(200, body)
    agent, session = _session(channel)
    with pytest.raises(RuntimeError):
        await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)


@pytest.mark.parametrize("channel", CHANNELS)
async def test_http_error_is_retry_visible(configured, wire, channel: str) -> None:
    wire(503, {"ok": True, "errcode": 0, "code": 0, "id": "id", "processQueryKey": "query"})
    agent, session = _session(channel)
    with pytest.raises(httpx.HTTPStatusError):
        await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)


@pytest.mark.parametrize(
    ("channel", "body"),
    [
        ("slack", {"ok": True}),
        ("wecom", {"errcode": 0}),
        ("dingtalk", {"processQueryKey": "query"}),
        ("feishu", {"code": 0, "data": {"message_id": "id"}}),
        ("whatsapp", {"messages": [{"id": "id"}]}),
        ("discord", {"id": "id"}),
        ("microsoft_teams", {"id": "id"}),
        ("google_chat", {"name": "spaces/space/messages/id"}),
        ("wechat", {"ret": 0}),
    ],
)
async def test_provider_success_completes(configured, wire, channel, body) -> None:
    requests = wire(200, body)
    agent, session = _session(channel)
    await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)
    assert requests[-1].method == "POST"


async def test_default_remains_best_effort(configured, wire) -> None:
    wire(503, {"ok": False})
    agent, session = _session("slack")
    await outbound.deliver_session_reply(agent=agent, session=session, content="report")


@pytest.mark.parametrize("channel", CHANNELS)
async def test_default_missing_config_remains_best_effort(monkeypatch, channel: str) -> None:
    async def missing(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(outbound.channel_config_dao, "get_configured_for_agent", missing)
    agent, session = _session(channel)
    await outbound.deliver_session_reply(agent=agent, session=session, content="report")


async def test_strict_checks_each_chunk(monkeypatch, configured) -> None:
    from app.api import slack

    original = httpx.AsyncClient
    count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200, json={"ok": count == 1})

    def client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(slack, "SLACK_MSG_LIMIT", 3)
    agent, session = _session("slack")
    with pytest.raises(RuntimeError):
        await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)
    assert count == 2


async def test_strict_waits_for_send_and_preserves_cancellation(monkeypatch, configured) -> None:
    from app.api import slack

    started = asyncio.Event()
    release = asyncio.Event()

    async def send(*args: object, **kwargs: object) -> None:
        started.set()
        await release.wait()

    monkeypatch.setattr(slack, "_send_slack_messages", send)
    agent, session = _session("slack")
    task = asyncio.create_task(
        outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_config_lookup_closes_before_send(monkeypatch) -> None:
    active = False
    agent, session = _session("slack")

    class Connection:
        async def fetchone(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "id": uuid4(),
                "agent_id": agent.id,
                "channel_type": "slack",
                "app_secret": "secret",
                "app_id": None,
                "encrypt_key": None,
                "verification_token": None,
            }

    @asynccontextmanager
    async def db_session() -> AsyncGenerator[Connection]:
        nonlocal active
        active = True
        try:
            yield Connection()
        finally:
            active = False

    from app.api import slack

    async def send(*args: object, **kwargs: object) -> None:
        assert not active

    monkeypatch.setattr(outbound.channel_config_dao, "session", db_session)
    monkeypatch.setattr(slack, "_send_slack_messages", send)
    await outbound.deliver_session_reply(agent=agent, session=session, content="report", strict=True)
