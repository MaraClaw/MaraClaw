from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

from app.api import background_tasks, feishu as feishu_api
from app.db import session as session_module
from app.records.channel_config import ChannelConfigRecord
from app.schemas.schemas import ChannelConfigCreate
from test_db_session import _FakePool, _FakeRawConnection

AGENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.mark.asyncio
async def test_webhook_echoes_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        feishu_api.channel_config_dao,
        "get_for_agent",
        AsyncMock(return_value=SimpleNamespace(verification_token="token", encrypt_key="", extra_config={})),
    )
    request = Request(
        {"type": "http", "headers": []},
        AsyncMock(
            return_value={
                "type": "http.request",
                "body": b'{"type":"url_verification","token":"token","challenge":"verify-me"}',
            }
        ),
    )

    result = await feishu_api.feishu_event_webhook(AGENT_ID, request)

    assert result == {"challenge": "verify-me"}


@pytest.mark.asyncio
async def test_process_feishu_event_skips_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=True))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 0, "msg": "already processed"}


@pytest.mark.asyncio
async def test_process_feishu_event_missing_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {"header": {"event_id": "evt-2", "event_type": "im.message.receive_v1"}},
    )

    assert result == {"code": 1, "msg": "Channel not found"}


@pytest.mark.asyncio
async def test_process_text_group_message_uses_chat_and_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(app_id="cli_app", app_secret="secret")
    logs: list[str] = []
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(feishu_api.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(feishu_api.channel_inbound, "load_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(feishu_api.logger, "info", lambda msg, *args, **kwargs: logs.append(str(msg)))

    result = await feishu_api.process_feishu_event(
        AGENT_ID,
        {
            "header": {"event_id": "evt-3", "event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "message_type": "text",
                    "chat_type": "group",
                    "chat_id": "oc_group",
                    "content": '{"text":"hi"}',
                },
                "sender": {"sender_id": {"open_id": "ou_open", "user_id": "ou_tenant_user"}},
            },
        },
    )

    assert result == {"code": 1, "msg": "Agent not found"}
    assert any("chat_type=group" in line and "user_id_from_event='ou_tenant_user'" in line for line in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["file", "image"])
async def test_file_webhook_acks_blocked_work_and_logs_background_failure(monkeypatch, message_type):
    # Given
    monkeypatch.setattr(feishu_api.channel_dedup, "already_processed_shared", AsyncMock(return_value=False))
    monkeypatch.setattr(
        feishu_api.channel_config_dao,
        "get_for_agent",
        AsyncMock(
            return_value=SimpleNamespace(
                app_id="app", app_secret="secret", verification_token="token", encrypt_key="", extra_config={}
            ),
        ),
    )
    monkeypatch.setattr(feishu_api, "_load_agent_and_model", AsyncMock(return_value=(None, None, None)))
    payload = json.dumps(
        {
            "schema": "2.0",
            "header": {"event_id": "file-event", "event_type": "im.message.receive_v1", "token": "token"},
            "event": {"message": {"message_type": message_type}, "sender": {"sender_id": {"open_id": "user"}}},
        }
    ).encode()
    signature = hashlib.sha256(b"123nonce" + payload).hexdigest().encode()
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"x-lark-request-timestamp", b"123"),
                (b"x-lark-request-nonce", b"nonce"),
                (b"x-lark-signature", signature),
            ],
        },
        AsyncMock(return_value={"type": "http.request", "body": payload}),
    )
    started, release, observed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    failure = RuntimeError("file processing failed")
    logger = Mock()
    logger.opt.return_value.error.side_effect = lambda _: observed.set()
    monkeypatch.setattr(background_tasks, "logger", logger)

    async def process(*args):
        started.set()
        await release.wait()
        raise failure

    monkeypatch.setattr(feishu_api, "_handle_feishu_file", process)
    # When
    try:
        result = await asyncio.wait_for(feishu_api.feishu_event_webhook(AGENT_ID, request), 1)
        await asyncio.wait_for(started.wait(), 1)
        # Then
        assert result == {"code": 0, "msg": "ok"}
        assert not observed.is_set()
    finally:
        release.set()
        await asyncio.wait_for(observed.wait(), 1)
    logger.opt.assert_called_once_with(exception=failure)
    logger.opt.return_value.error.assert_called_once_with("Background task failed: Feishu handle file")


@pytest.mark.asyncio
@pytest.mark.parametrize(("existing", "mode"), [(False, "websocket"), (True, "websocket"), (True, "webhook")])
async def test_config_connector_waits_for_save_and_commit(monkeypatch, existing, mode):
    # Given
    from app.services.feishu_ws import feishu_ws_manager

    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(feishu_api, "check_agent_access", AsyncMock(return_value=(Mock(), None)))
    monkeypatch.setattr(feishu_api, "is_agent_creator", lambda *_: True)
    config = ChannelConfigRecord(
        id=uuid.uuid4(),
        agent_id=AGENT_ID,
        channel_type="feishu",
        app_id="app",
        app_secret="secret",
        encrypt_key="",
        verification_token="",
        is_configured=True,
        is_connected=False,
        last_tested_at=None,
        extra_config={"connection_mode": mode},
        created_at=None,
    )
    monkeypatch.setattr(
        feishu_api.channel_config_dao,
        "get_for_agent",
        AsyncMock(
            return_value=config if existing else None,
        ),
    )
    saved, started = asyncio.Event(), asyncio.Event()
    seen = []

    async def save(**kwargs):
        saved.set()
        return config

    async def connect(*args):
        seen.append((saved.is_set(), raw.commits))
        started.set()

    monkeypatch.setattr(feishu_api.channel_config_dao, "update", save)
    monkeypatch.setattr(feishu_api.channel_config_dao, "create", save)
    monkeypatch.setattr(feishu_ws_manager, "start_client" if mode == "websocket" else "stop_client", connect)
    data = ChannelConfigCreate(
        app_id="app", app_secret="secret", verification_token="token", extra_config={"connection_mode": mode}
    )
    token = session_module._conn_ctx.set(None)
    # When
    try:
        async with session_module.connection_ctx():
            result = await feishu_api.configure_channel(AGENT_ID, data, Mock())
            barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(barrier.set)
            await barrier.wait()
            assert not started.is_set()
        await asyncio.wait_for(started.wait(), 1)
    finally:
        session_module._conn_ctx.reset(token)
    # Then
    assert result.agent_id == AGENT_ID
    assert seen == [(True, 1)]
