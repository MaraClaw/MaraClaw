import asyncio
import json
import threading
from uuid import uuid4

import anyio
import dingtalk_stream
import httpx
import pytest

from app.api import dingtalk
from app.services import dingtalk_stream as dingtalk_media
from app.services.channels.media_limits import MediaTooLargeError
from app.services.dingtalk_stream import DingTalkStreamManager
from channel_media_fixtures import serve


@pytest.mark.parametrize("fails", [False, True])
async def test_sdk_thread_ack_waits_for_main_loop_acceptance(monkeypatch, fails):
    # Given
    manager = DingTalkStreamManager()
    manager._main_loop = asyncio.get_running_loop()
    stop = threading.Event()
    started, release = anyio.Event(), anyio.Event()
    acknowledgements, committed = [], []

    async def accept(**kwargs):
        assert asyncio.get_running_loop() is manager._main_loop
        started.set()
        await release.wait()
        if fails:
            raise RuntimeError("database unavailable")
        committed.append(kwargs["message_id"])

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
    monkeypatch.setattr(dingtalk_stream.DingTalkStreamClient, "start_forever", start)
    # When
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(anyio.to_thread.run_sync, manager._run_client_thread, uuid4(), "app", "secret", stop)
        with anyio.fail_after(3):
            await started.wait()
        assert acknowledgements == committed == []
        release.set()
    # Then
    assert committed == ([] if fails else ["native-id"])
    assert [ack["code"] for ack in acknowledgements] == [
        dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION if fails else dingtalk_stream.AckMessage.STATUS_OK
    ]
    assert not manager._children.tasks


async def test_dispatch_on_application_loop_awaits_without_deadlock():
    # Given
    manager = DingTalkStreamManager()
    accepted = []

    async def accept():
        accepted.append("committed")

    # When
    with anyio.fail_after(1):
        await manager._dispatch(asyncio.get_running_loop(), accept())
    # Then
    assert accepted == ["committed"]


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
            await manager._dispatch(loop, accept())
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
