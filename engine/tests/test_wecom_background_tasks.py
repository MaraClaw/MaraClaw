import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from starlette.requests import Request

from app.api import background_tasks, wecom
from app.db import session as session_module
from app.records.channel_config import ChannelConfigRecord
from app.services.channels import dedup
from test_db_session import _FakePool, _FakeRawConnection


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "kf"])
@pytest.mark.parametrize("fails", [False, True])
async def test_webhook_waits_for_acceptance_and_retries_failure(monkeypatch, kind, fails):
    # Given
    agent_id = uuid4()
    key = str(uuid4())
    config = SimpleNamespace(verification_token="token", encrypt_key="key")
    request = Request(
        {"type": "http"},
        AsyncMock(
            return_value={
                "type": "http.request",
                "body": b"<xml><Encrypt>cipher</Encrypt></xml>",
            }
        ),
    )
    content = (
        f"<MsgType>text</MsgType><MsgId>{key}</MsgId><Content>hello</Content>"
        if kind == "text"
        else f"<MsgType>event</MsgType><Event>kf_msg_or_event</Event><Token>{key}</Token>"
    )
    xml = f"<xml><FromUserName>user</FromUserName>{content}</xml>"
    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=config))
    monkeypatch.setattr(wecom, "_decrypt_msg", lambda *_: (xml, "corp"))
    monkeypatch.setattr(wecom, "_verify_signature", lambda *_: "signature")
    started, release = asyncio.Event(), asyncio.Event()
    committed = []
    attempts = []

    async def process(*args, **kwargs):
        attempts.append(key)
        started.set()
        await release.wait()
        if fails and len(attempts) == 1:
            raise RuntimeError("database unavailable")
        committed.append(key)

    monkeypatch.setattr(dedup, "already_processed_shared", AsyncMock(return_value=True))
    mark = AsyncMock()
    monkeypatch.setattr(dedup, "mark_processed_shared", mark)
    scheduled = Mock()
    monkeypatch.setattr(wecom, "schedule_background_task", scheduled)
    monkeypatch.setattr(wecom, "_process_wecom_text" if kind == "text" else "_process_wecom_kf_event", process)
    # When
    pending = asyncio.create_task(wecom.wecom_event_webhook(agent_id, request, "signature"))
    await asyncio.wait_for(started.wait(), 1)
    assert not pending.done()
    assert committed == []
    release.set()
    response = await asyncio.wait_for(pending, 1)
    # Then
    assert response.status_code == (503 if fails else 200)
    assert committed == ([] if fails else [key])
    if fails:
        retry = await wecom.wecom_event_webhook(agent_id, request, "signature")
        assert retry.status_code == 200
        assert committed == [key]
    scheduled.assert_not_called()
    mark.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["websocket", "webhook"])
@pytest.mark.parametrize("existing", [True, False])
async def test_config_connector_runs_after_save_and_commit(monkeypatch, mode, existing):
    # Given
    agent_id = uuid4()
    raw = _FakeRawConnection()
    monkeypatch.setattr(session_module, "get_pool", lambda: _FakePool(raw))
    monkeypatch.setattr(wecom, "check_agent_access", AsyncMock(return_value=(object(), None)))
    monkeypatch.setattr(wecom, "is_agent_creator", lambda *_: True)
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent_id,
        channel_type="wecom",
        app_id="corp",
        app_secret="secret",
        encrypt_key="key",
        verification_token="token",
        is_configured=True,
        is_connected=False,
        last_tested_at=None,
        extra_config={},
        created_at=None,
    )
    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=config if existing else None))
    saved = asyncio.Event()

    async def save(**kwargs):
        saved.set()
        return config

    monkeypatch.setattr(wecom.channel_config_dao, "update", save)
    monkeypatch.setattr(wecom.channel_config_dao, "create", save)
    started = asyncio.Event()
    seen = []

    async def connect(*args):
        seen.append((saved.is_set(), raw.commits))
        started.set()

    operation = "start_client" if mode == "websocket" else "stop_client"
    monkeypatch.setattr(wecom.wecom_stream_manager, operation, connect)
    data: wecom.WeComChannelPayload = (
        {"bot_id": "bot", "bot_secret": "secret"}
        if mode == "websocket"
        else {
            "corp_id": "corp",
            "secret": "secret",
            "token": "token",
            "encoding_aes_key": "key",
        }
    )
    token = session_module._conn_ctx.set(None)
    # When
    try:
        async with session_module.connection_ctx():
            result = await wecom.configure_wecom_channel(agent_id, data, Mock())
            # An event-loop barrier lets incorrectly eager launchers run before commit.
            barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(barrier.set)
            await barrier.wait()
            assert not started.is_set()
        await asyncio.wait_for(started.wait(), 1)
    finally:
        session_module._conn_ctx.reset(token)
    # Then
    assert result.agent_id == agent_id
    assert seen == [(True, 1)]


@pytest.mark.asyncio
async def test_connector_background_failure_is_logged(monkeypatch):
    # Given
    agent_id = uuid4()
    config = ChannelConfigRecord(
        id=uuid4(),
        agent_id=agent_id,
        channel_type="wecom",
        app_id="",
        app_secret="",
        encrypt_key="",
        verification_token="",
        is_configured=True,
        is_connected=False,
        last_tested_at=None,
        extra_config={},
        created_at=None,
    )
    monkeypatch.setattr(wecom, "check_agent_access", AsyncMock(return_value=(object(), None)))
    monkeypatch.setattr(wecom, "is_agent_creator", lambda *_: True)
    monkeypatch.setattr(wecom.channel_config_dao, "get_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(wecom.channel_config_dao, "create", AsyncMock(return_value=config))
    failure = RuntimeError("connector failed")
    monkeypatch.setattr(wecom.wecom_stream_manager, "start_client", AsyncMock(side_effect=failure))
    observed = asyncio.Event()
    logger = Mock()
    logger.opt.return_value.error.side_effect = lambda _: observed.set()
    monkeypatch.setattr(background_tasks, "logger", logger)
    # When
    result = await wecom.configure_wecom_channel(agent_id, {"bot_id": "bot", "bot_secret": "secret"}, Mock())
    await asyncio.wait_for(observed.wait(), 1)
    # Then
    assert result.agent_id == agent_id
    logger.opt.assert_called_once_with(exception=failure)
    logger.opt.return_value.error.assert_called_once_with("Background task failed: WeCom start_client")
