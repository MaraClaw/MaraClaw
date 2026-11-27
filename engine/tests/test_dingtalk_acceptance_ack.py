import asyncio
import json
import threading
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import dingtalk_stream
import httpx
import pytest
from fastapi import HTTPException
from tests.test_channel_shared_processors import context, pipeline

from app.api import dingtalk
from app.core.domain.channels import ChannelType
from app.services import dingtalk_stream as dingtalk_media
from app.services.channels import binding, submissions
from app.services.channels.dingtalk_stream_transport import DingTalkTransport
from app.services.channels.media_limits import MediaTooLargeError
from app.services.channels.stream_source import stream_source
from app.services.dingtalk_stream import DingTalkStreamManager
from channel_media_fixtures import serve
from stream_source_fixtures import connection_config

__all__ = ["context", "pipeline"]


def bot_message(kind, text="hello", **fields):
    return {
        "msgtype": kind,
        "senderStaffId": "staff-id",
        "conversationId": "chat-id",
        "text": {"content": text},
        "content": {"downloadCode": "code", "fileName": "capture.png"},
        **fields,
    }


async def deliver(monkeypatch, agent_id, messages):
    """Send each message through the real SDK client of a started manager and return the acknowledgement codes."""
    manager, done, loop = DingTalkStreamManager(), asyncio.Event(), asyncio.get_running_loop()
    acknowledgements = []

    async def send(payload):
        acknowledgements.append(json.loads(payload)["code"])

    def run(transport, client):
        client.websocket = type("Socket", (), {"send": staticmethod(send)})()

        async def receive():
            for index, data in enumerate(messages):
                await client.route_message(
                    {
                        "type": "CALLBACK",
                        "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": f"delivery-{index}"},
                        "data": json.dumps(data),
                    }
                )

        try:
            asyncio.run(receive())
        finally:
            manager._stop_events[agent_id].set()
            loop.call_soon_threadsafe(done.set)

    monkeypatch.setattr(DingTalkTransport, "run", run)
    try:
        await manager.start_client(agent_id, "app", "secret")
        await asyncio.wait_for(done.wait(), 3)
    finally:
        await manager.stop_all()
    return acknowledgements


@pytest.mark.parametrize("fails", [False, True])
async def test_sdk_thread_ack_waits_for_main_loop_acceptance(monkeypatch, fails):
    # Given
    manager = DingTalkStreamManager()
    manager._main_loop = asyncio.get_running_loop()
    stop = threading.Event()
    started, release = anyio.Event(), anyio.Event()
    acknowledgements, committed = [], []
    agent_id = uuid4()
    config = connection_config(monkeypatch, agent_id, "dingtalk")

    async def accept(**kwargs):
        assert asyncio.get_running_loop() is manager._main_loop
        started.set()
        await release.wait()
        if fails:
            raise RuntimeError("database unavailable")
        committed.append(json.loads(kwargs["native"].event_key))

    async def send(payload):
        acknowledgements.append(json.loads(payload))

    def start(client):
        client.websocket = type("Socket", (), {"send": staticmethod(send)})()
        callback = {
            "type": "CALLBACK",
            "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": "delivery-id"},
            "data": json.dumps({"msgtype": "text", "msgId": "native-id", "text": {"content": "hello"}}),
        }
        try:
            asyncio.run(client.route_message(callback))
        finally:
            stop.set()

    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    monkeypatch.setattr(DingTalkTransport, "run", lambda self, client: start(client))
    # When
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(
            anyio.to_thread.run_sync,
            manager._run_client_thread,
            agent_id,
            "app",
            "secret",
            stop,
            None,
            stream_source(config),
        )
        with anyio.fail_after(3):
            await started.wait()
        assert acknowledgements == committed == []
        release.set()
    # Then
    assert committed == ([] if fails else [["app", "", "native-id"]])
    assert [ack["code"] for ack in acknowledgements] == [
        dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION if fails else dingtalk_stream.AckMessage.STATUS_OK
    ]
    assert not manager._children.tasks


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
@pytest.mark.parametrize("kind", ["text", "picture"])
async def test_actual_thread_burst_bounds_enrichment_and_acknowledges_only_commits(monkeypatch, outcome, kind):
    manager, agent_id = DingTalkStreamManager(), uuid4()
    loop = asyncio.get_running_loop()
    started, refused, completed, release = (asyncio.Event() for _ in range(4))
    entered, enriched, committed, acknowledgements, clients = [], [], [], [], []
    failing, restarting = outcome == "failure", False

    connection_config(monkeypatch, agent_id, "dingtalk")

    async def replay(event):
        return False

    async def media(**kwargs):
        enriched.append(kwargs)
        return "hello", None, None

    async def accept(**kwargs):
        assert asyncio.get_running_loop() is loop
        entered.append(kwargs)
        if len(entered) == 8:
            started.set()
        await release.wait()
        if failing:
            raise RuntimeError("durable acceptance failed")
        committed.append(kwargs)

    async def send(payload):
        acknowledgements.append(json.loads(payload)["code"])
        if len(acknowledgements) == 24:
            loop.call_soon_threadsafe(refused.set)

    def callback(index):
        return {
            "type": "CALLBACK",
            "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": f"delivery-{index}"},
            "data": json.dumps(
                {
                    "msgtype": kind,
                    "msgId": f"native-{index}",
                    "text": {"content": "hello"},
                    "content": {"downloadCode": "download"},
                }
            ),
        }

    def start(client):
        clients.append(client)
        stop = manager._stop_events[agent_id]
        client.websocket = type("Socket", (), {"send": staticmethod(send)})()

        async def receive():
            await asyncio.gather(*(client.route_message(callback(index)) for index in range(1 if restarting else 32)))

        try:
            asyncio.run(receive())
        finally:
            stop.set()
            loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr(dingtalk_media, "is_channel_event_replay", replay)
    monkeypatch.setattr(dingtalk_media, "_process_media_message", media)
    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    monkeypatch.setattr(DingTalkTransport, "run", lambda self, client: start(client))
    try:
        await manager.start_client(agent_id, "app", "secret")
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(refused.wait(), 2)
        assert len(entered) == 8
        assert len(enriched) == (8 if kind == "picture" else 0)
        assert committed == []
        assert acknowledgements == [dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION] * 24
        if outcome == "cancel":
            await manager.stop_client(agent_id)
        else:
            release.set()
        await asyncio.wait_for(completed.wait(), 2)
        await manager.stop_client(agent_id)
        assert len(committed) == (8 if outcome == "success" else 0)
        assert acknowledgements.count(dingtalk_stream.AckMessage.STATUS_OK) == (8 if outcome == "success" else 0)
        restarting, failing = True, False
        completed.clear()
        release.set()
        await manager.start_client(agent_id, "app", "secret")
        await asyncio.wait_for(completed.wait(), 2)
        await clients[0].route_message(callback("stale"))
        assert len(entered) == 9
        assert acknowledgements[-1] == dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION
    finally:
        release.set()
        await manager.stop_all()


async def test_dispatch_on_application_loop_awaits_without_deadlock():
    # Given
    manager = DingTalkStreamManager()
    accepted = []

    async def accept():
        accepted.append("committed")

    # When
    with anyio.fail_after(1):
        await manager._dispatch(asyncio.get_running_loop(), accept)
    # Then
    assert accepted == ["committed"]


@pytest.mark.parametrize("kind", ["text", "picture"])
async def test_connected_sdk_refuses_rotated_text_and_media_sources_before_new_effects(pipeline, monkeypatch, kind):
    config = pipeline[4]
    config.channel_type = ChannelType.DINGTALK
    config.app_id, config.app_secret = "app", "secret"
    manager, done = DingTalkStreamManager(), asyncio.Event()
    loop = asyncio.get_running_loop()
    acknowledgements, enriched = [], []
    original = submissions.accept_channel_event

    async def accept(event, **kwargs):
        result = await original(event, **kwargs)
        config.app_secret = "rotated"
        return result

    async def media(**kwargs):
        enriched.append(kwargs["source"])
        return "hello", None, None

    async def send(payload):
        acknowledgements.append(json.loads(payload)["code"])

    def run(transport, client):
        client.websocket = type("Socket", (), {"send": staticmethod(send)})()

        async def receive():
            for index in range(2):
                await client.route_message(
                    {
                        "type": "CALLBACK",
                        "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": f"delivery-{index}"},
                        "data": json.dumps(
                            {
                                "msgtype": kind,
                                "msgId": f"native-{index}",
                                "senderStaffId": "staff-id",
                                "conversationId": "chat-id",
                                "text": {"content": "hello"},
                                "content": {"downloadCode": "code"},
                            }
                        ),
                    }
                )

        try:
            asyncio.run(receive())
        finally:
            manager._stop_events[config.agent_id].set()
            loop.call_soon_threadsafe(done.set)

    monkeypatch.setattr(dingtalk, "accept_channel_event", accept)
    monkeypatch.setattr(dingtalk_media, "_process_media_message", media)
    monkeypatch.setattr(DingTalkTransport, "run", run)
    try:
        await manager.start_client(config.agent_id, "app", "secret")
        await asyncio.wait_for(done.wait(), 3)
        assert acknowledgements == [
            dingtalk_stream.AckMessage.STATUS_OK,
            dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION,
        ]
        assert len(enriched) == (1 if kind == "picture" else 0)
        pipeline[-2].assert_awaited_once()
        pipeline[-1].assert_awaited_once()
    finally:
        await manager.stop_all()


@pytest.mark.parametrize("identity", [{}, {"msgId": ""}, {"msgId": None}])
@pytest.mark.parametrize("kind", ["text", "picture", "file"])
async def test_message_without_an_id_is_acknowledged_before_any_replay_check_download_or_acceptance(
    monkeypatch, kind, identity
):
    # Given: a stream whose messages carry no DingTalk message ID, then one that does
    agent_id, effects = uuid4(), []
    connection_config(monkeypatch, agent_id, "dingtalk")

    async def replay(event):
        effects.append(("replay", event.event_key))
        return False

    async def media(**kwargs):
        effects.append(("media", None))
        return "hello", None, None

    async def accept(**kwargs):
        effects.append(("accept", kwargs["native"].event_key))

    monkeypatch.setattr(dingtalk_media, "is_channel_event_replay", replay)
    monkeypatch.setattr(dingtalk_media, "_process_media_message", media)
    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    # When: two different messages of one conversation arrive without an ID, then a normal one
    acknowledgements = await deliver(
        monkeypatch,
        agent_id,
        [
            bot_message(kind, "first", **identity),
            bot_message(kind, "second", **identity),
            bot_message(kind, "third", msgId="native-1"),
        ],
    )
    # Then: all three are acknowledged, and only the one with an ID reached replay check, download and acceptance
    key = '["app","chat-id","native-1"]'
    assert acknowledgements == [dingtalk_stream.AckMessage.STATUS_OK] * 3
    assert effects == ([("accept", key)] if kind == "text" else [("replay", key), ("media", None), ("accept", key)])


@pytest.mark.parametrize("kind", ["text", "picture"])
@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        ("unbound", dingtalk_stream.AckMessage.STATUS_OK),
        ("unauthorized", dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION),
    ],
)
async def test_unlinked_sender_is_acknowledged_and_ignored_but_any_other_refusal_still_fails(
    pipeline, monkeypatch, kind, refusal, code
):
    # Given: a sender who is not a linked member, or one whose access to the agent is refused
    config = pipeline[4]
    config.channel_type = ChannelType.DINGTALK
    config.app_id, config.app_secret = "app", "secret"
    *_, persist, enqueue = pipeline
    downloads, reserve = [], AsyncMock(return_value=True)

    async def media(**kwargs):
        downloads.append(kwargs["source"])
        return "hello", None, None

    monkeypatch.setattr(dingtalk_media, "_process_media_message", media)
    monkeypatch.setattr(submissions.channel_event_receipt_dao, "reserve", reserve)
    if refusal == "unbound":
        monkeypatch.setattr(binding.channel_user_service, "_find_org_member", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr(binding, "check_agent_access", AsyncMock(side_effect=HTTPException(403)))
    # When
    acknowledgements = await deliver(monkeypatch, config.agent_id, [bot_message(kind, msgId="native-1")])
    # Then: only the unlinked sender is acknowledged; nothing was downloaded, reserved, stored or queued either way
    assert acknowledgements == [code]
    assert downloads == []
    reserve.assert_not_awaited()
    persist.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.parametrize("unavailable", ["absent", "stopped", "closed", "closing"])
async def test_dispatch_rejects_unavailable_acceptance_loop(unavailable):
    # Given
    manager = DingTalkStreamManager()
    loop = asyncio.new_event_loop() if unavailable in {"stopped", "closed"} else None
    if unavailable == "closed":
        assert loop is not None
        loop.close()
    if unavailable == "closing":
        manager._closing = True
        loop = asyncio.get_running_loop()
    accepted = []

    async def accept():
        accepted.append("committed")

    # When / Then
    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            await manager._dispatch(loop, accept)
        assert accepted == []
    finally:
        if loop is not None and unavailable == "stopped":
            loop.close()


async def test_download_file_signals_oversize_and_permanent_failures_but_lets_a_5xx_propagate(monkeypatch):
    # Given: a provider that serves 503, 403 and an oversize body from three URLs
    def handler(request):
        if request.url.path == "/unavailable":
            return httpx.Response(503)
        if request.url.path == "/forbidden":
            return httpx.Response(403)
        return httpx.Response(200, content=b"x" * 16)

    serve(monkeypatch, handler)
    # When / Then: oversize has its own signal, a 5xx keeps its retry signal, an expired URL still reports None
    with pytest.raises(httpx.HTTPStatusError):
        await dingtalk_media._download_file("https://files.invalid/unavailable", 1024)
    assert await dingtalk_media._download_file("https://files.invalid/forbidden", 1024) is None
    with pytest.raises(MediaTooLargeError):
        await dingtalk_media._download_file("https://files.invalid/large", 8)
    assert await dingtalk_media._download_file("https://files.invalid/large", 16) == b"x" * 16


async def test_download_file_lets_a_timeout_propagate(monkeypatch):
    # Given
    def handler(request):
        raise httpx.ReadTimeout("slow provider", request=request)

    serve(monkeypatch, handler)
    # When / Then
    with pytest.raises(httpx.ReadTimeout):
        await dingtalk_media._download_file("https://files.invalid/slow", 1024)


async def test_media_download_url_lookup_lets_a_5xx_propagate_and_reports_a_refusal_as_none(monkeypatch):
    # Given: DingTalk answers the lookup with 503, then a 403 error body, then a download URL
    answers = iter(
        [
            httpx.Response(503, text="<html>bad gateway</html>"),
            httpx.Response(403, json={"code": "Forbidden.AccessDenied"}),
            httpx.Response(200, json={"downloadUrl": "https://files.invalid/f"}),
        ]
    )
    serve(monkeypatch, lambda _: next(answers))
    # When / Then
    with pytest.raises(httpx.HTTPStatusError):
        await dingtalk_media._get_media_download_url("token", "code", "robot")
    assert await dingtalk_media._get_media_download_url("token", "code", "robot") is None
    assert await dingtalk_media._get_media_download_url("token", "code", "robot") == "https://files.invalid/f"
