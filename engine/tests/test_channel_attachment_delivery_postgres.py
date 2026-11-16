import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from app.api import background_tasks, slack
from app.api.websocket import manager
from app.config import get_settings
from app.services.channels import binding, slack_submissions, submissions
from app.services.channels.binding import _publish_shared_user_message
from app.services.openclaw_routing import PreparedOpenClawMessage
from app.services.shared_chat import shared_session_id
from app.services.storage_runtime import facade
from channel_shared_postgres_fixtures import live_channel, schema_db

__all__ = ["live_channel", "schema_db"]


@pytest.mark.parametrize("is_group", [False, True])
@pytest.mark.parametrize("subtype", [None, "file_share"])
async def test_signed_im_attachment_persists_and_publishes_reference_once(
    live_channel, monkeypatch, tmp_path, is_group, subtype
):
    live = live_channel
    settings = get_settings()
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(settings, "STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setattr(facade, "_storage_backend", None)
    monkeypatch.setattr(binding, "_publish_shared_user_message", _publish_shared_user_message)
    published = AsyncMock()
    monkeypatch.setattr(manager, "send_to_session", published)

    async def prepare(*, agent, content, history):
        return PreparedOpenClawMessage(agent, content, "primary", None, "attachment-qa")

    monkeypatch.setattr(submissions, "prepare_shared_openclaw_message", prepare)
    data = b"office attachment payload"
    downloads = 0

    def download(request):
        nonlocal downloads
        downloads += 1
        assert request.url.host == "files.slack.com"
        return httpx.Response(200, content=data)

    @asynccontextmanager
    async def http_client(**_kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
            yield client

    monkeypatch.setattr(slack_submissions, "public_http_client", http_client)
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "attachment-event",
            "event": {
                "type": "message",
                "subtype": subtype,
                "user": "U0",
                "channel": "C0" if is_group else "D0",
                "text": "",
                "files": [{"id": "F0", "name": "quarterly.xlsx", "url_private": "https://files.slack.com/report"}],
            },
        }
    ).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"qa-secret", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": stamp, "x-slack-signature": f"v0={signature}"}

    app = FastAPI()
    app.include_router(slack.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://qa") as client:
        responses = [
            await client.post(f"/api/channel/slack/{live.agent.id}/webhook", content=body, headers=headers)
            for _ in range(2)
        ]
    await background_tasks.drain_background_tasks()

    assert [response.status_code for response in responses] == [200, 200]
    assert downloads == 1
    refs = [{"path": "workspace/uploads/quarterly.xlsx", "name": "quarterly.xlsx"}]
    rows = await live.db.fetchall("SELECT content, attachments FROM chat_messages")
    assert rows == [{"content": "[file:workspace/uploads/quarterly.xlsx]", "attachments": refs}]
    published.assert_awaited_once()
    assert published.await_args is not None
    agent_id, conversation_id, event = published.await_args.args
    assert agent_id == str(live.agent.id)
    if is_group:
        assert conversation_id != str(shared_session_id(live.agent.id))
        session = await live.db.fetchone(
            "SELECT is_group FROM chat_sessions WHERE id = %(id)s", {"id": conversation_id}
        )
        assert session == {"is_group": True}
    else:
        assert conversation_id == str(shared_session_id(live.agent.id))
    assert event["session_id"] == conversation_id
    assert event["role"] == "user"
    assert event["attachments"] == refs
    assert "office attachment payload" not in json.dumps(event)
    assert await facade.get_storage_backend().read_bytes(f"{live.agent.id}/{refs[0]['path']}") == data
