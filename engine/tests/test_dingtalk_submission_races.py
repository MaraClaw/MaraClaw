import asyncio
import json
import threading
from uuid import uuid4

import dingtalk_stream
import pytest

from app.api import dingtalk
from app.services.channels.dingtalk_stream_transport import DingTalkTransport
from app.services.dingtalk_stream import DingTalkStreamManager
from stream_source_fixtures import connection_config


@pytest.mark.parametrize("cancel_sdk", [False, True])
async def test_sdk_burst_racing_shutdown_never_starts_pending_application_work(monkeypatch, cancel_sdk):
    manager, agent_id = DingTalkStreamManager(), uuid4()
    connection_config(monkeypatch, agent_id, "dingtalk")
    refused = threading.Event()
    acknowledgements, entered = [], []

    async def accept(**kwargs):
        entered.append(kwargs)

    async def send(payload):
        acknowledgements.append(json.loads(payload)["code"])
        if len(acknowledgements) == 24:
            refused.set()

    def start(client):
        stop = manager._stop_events[agent_id]
        client.websocket = type("Socket", (), {"send": staticmethod(send)})()

        async def receive():
            deliveries = [
                asyncio.create_task(
                    client.route_message(
                        {
                            "type": "CALLBACK",
                            "headers": {"topic": dingtalk_stream.ChatbotMessage.TOPIC, "messageId": str(index)},
                            "data": json.dumps({"msgtype": "text", "msgId": str(index), "text": {"content": "hello"}}),
                        }
                    )
                )
                for index in range(32)
            ]
            await asyncio.sleep(0)
            if cancel_sdk:
                for delivery in deliveries[:8]:
                    delivery.cancel()
            await asyncio.gather(*deliveries)

        try:
            asyncio.run(receive())
        finally:
            stop.set()

    monkeypatch.setattr(dingtalk, "process_dingtalk_message", accept)
    monkeypatch.setattr(DingTalkTransport, "run", lambda self, client: start(client))
    try:
        await manager.start_client(agent_id, "app", "secret")
        thread = manager._threads[agent_id]
        # Hold the application loop until the SDK has posted eight reservations and refused the rest.
        assert refused.wait(2)
        assert entered == []
        await manager.stop_client(agent_id)
        assert entered == []
        assert acknowledgements == [dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION] * 32
        assert not thread.is_alive()
    finally:
        await manager.stop_all()
