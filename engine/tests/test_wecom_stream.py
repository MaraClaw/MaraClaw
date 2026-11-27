import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import override
from unittest.mock import AsyncMock, call

import pytest

from app.core.json_types import JsonObject
from app.services import wecom_stream
from app.services.channel_user_service import ChannelUserResolutionError
from app.services.channels import submissions, wecom_media
from app.services.wecom_stream import (
    WeComStreamManager,
    _build_wecom_conv_id,
    _extract_wecom_chat_id,
    _extract_wecom_chat_type,
    _extract_wecom_sender_id,
)
from stream_source_fixtures import connection_config


def test_extract_wecom_context_from_official_sdk_shape():
    body: JsonObject = {
        "msgid": "msg_123",
        "msgtype": "text",
        "from_userid": "zhangsan",
        "chattype": "group",
        "chatid": "chat_001",
        "text": {"content": "hello"},
    }

    assert _extract_wecom_sender_id(body) == "zhangsan"
    assert _extract_wecom_chat_type(body) == "group"
    assert _extract_wecom_chat_id(body) == "chat_001"
    assert _build_wecom_conv_id("zhangsan", "chat_001", "group") == "wecom_group_chat_001"


def test_extract_wecom_context_from_nested_legacy_shape():
    body: JsonObject = {
        "from": {"userid": "lisi"},
        "chat_type": "single",
        "chatid": "lisi",
        "text": {"content": "hi"},
    }

    assert _extract_wecom_sender_id(body) == "lisi"
    assert _extract_wecom_chat_type(body) == "single"
    assert _extract_wecom_chat_id(body) == "lisi"
    assert _build_wecom_conv_id("lisi", "lisi", "single") == "wecom_p2p_lisi"


def test_build_wecom_conv_id_falls_back_to_sender_for_missing_group_chat_id():
    assert _build_wecom_conv_id("wangwu", "", "group") == "wecom_p2p_wangwu"


def test_status_uses_connection_state_not_task_liveness():
    agent_id = uuid.uuid4()
    manager = WeComStreamManager()

    manager._connected[agent_id] = False

    assert manager.status() == {str(agent_id): False}


def test_status_reports_connected_agent():
    agent_id = uuid.uuid4()
    manager = WeComStreamManager()

    manager._connected[agent_id] = True

    assert manager.status() == {str(agent_id): True}


class FakeWSClient:
    def __init__(self, _options: object) -> None:
        self.handlers: dict[str, Callable[[object], Awaitable[None]]] = {}
        self.initial_connection = asyncio.Event()
        self.second_connection = asyncio.Event()
        self.block_second_connection = asyncio.Event()
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.fail_first_connection = False

    def on(self, event: str, handler: Callable[[object], Awaitable[None]]) -> None:
        self.handlers[event] = handler

    async def connect_async(self) -> None:
        self.connect_calls += 1
        if self.fail_first_connection and self.connect_calls == 1:
            raise RuntimeError("initial connection failed")
        if not self.initial_connection.is_set():
            self.initial_connection.set()
            return
        self.second_connection.set()
        await self.block_second_connection.wait()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def reply_stream(self, _frame: object, _stream_id: str, _content: str, *, finish: bool) -> None:
        del finish

    async def reply_welcome(self, _frame: object, _message: object) -> None:
        return


class ReplacementClient:
    def on(self, event: str, handler: Callable[[wecom_stream._WeComFrame], Awaitable[None]]) -> None:
        del event, handler

    async def connect_async(self) -> None:
        return

    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def reply_stream(
        self, frame: wecom_stream._WeComFrame, stream_id: str, content: str, *, finish: bool
    ) -> None:
        del frame, stream_id, content, finish

    async def reply_welcome(self, frame: wecom_stream._WeComFrame, message: JsonObject) -> None:
        del frame, message


class FakeWSClientOptions:
    def __init__(
        self,
        *,
        bot_id: str,
        secret: str,
        max_reconnect_attempts: int,
        heartbeat_interval: int,
    ) -> None:
        self.bot_id = bot_id
        self.secret = secret
        self.max_reconnect_attempts = max_reconnect_attempts
        self.heartbeat_interval = heartbeat_interval


class OptionsRecordingWSClient(FakeWSClient):
    def __init__(self, options: object) -> None:
        if not isinstance(options, FakeWSClientOptions):
            raise TypeError("WSClient requires WSClientOptions")
        super().__init__(options)
        self.options = options


class RecordingLogger:
    def __init__(self) -> None:
        self.debug_messages: list[str] = []
        self.error_messages: list[str] = []

    def debug(self, message: str) -> None:
        self.debug_messages.append(message)

    def info(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def opt(self, **_kwargs: object) -> RecordingLogger:
        return self

    def error(self, message: str) -> None:
        self.error_messages.append(message)


async def wait_forever() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_stream_client_constructs_sdk_options(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an SDK that only accepts its typed options object
    client: OptionsRecordingWSClient | None = None

    def create_client(options: object) -> OptionsRecordingWSClient:
        nonlocal client
        client = OptionsRecordingWSClient(options)
        return client

    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(
            WSClient=create_client,
            WSClientOptions=FakeWSClientOptions,
            generate_req_id=str,
        ),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()

    # When: the manager starts the SDK client
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.sleep(0)
    assert client is not None
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)

    # Then: the official options shape retains connector reconnect settings
    assert client.options.bot_id == "bot"
    assert client.options.secret == "secret"
    assert client.options.max_reconnect_attempts == -1
    assert client.options.heartbeat_interval == 30000

    await manager.stop_client(agent_id)


@pytest.mark.asyncio
async def test_sdk_disconnect_does_not_trigger_a_second_manager_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an SDK client whose first connection has succeeded
    client = FakeWSClient({})
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    original_sleep = asyncio.sleep

    async def advance_without_waiting(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(wecom_stream.asyncio, "sleep", advance_without_waiting)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)

    # When: the SDK reports a disconnect and begins its own reconnect process
    handler = client.handlers["disconnected"]
    await handler(object())

    # Then: the manager does not issue another connect_async call
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.second_connection.wait(), timeout=0.05)

    await manager.stop_client(agent_id)


@pytest.mark.asyncio
async def test_initial_connection_failure_is_retried_by_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the SDK rejects the first initial connection attempt
    client = FakeWSClient({})
    client.fail_first_connection = True
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    original_sleep = asyncio.sleep

    async def advance_without_waiting(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(wecom_stream.asyncio, "sleep", advance_without_waiting)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()

    # When: the stream client starts
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)

    # Then: the manager retries only the failed initial connection
    assert client.connect_calls == 2
    assert manager.status() == {str(agent_id): True}

    await manager.stop_client(agent_id)


class FailingConnectWSClient(FakeWSClient):
    def __init__(self, failures: int, *, park: Callable[[], Awaitable[None]] | None = None) -> None:
        super().__init__({})
        self.failures = failures
        self.park = park

    @override
    async def connect_async(self) -> None:
        self.connect_calls += 1
        if self.park is not None:
            await self.park()
        if self.connect_calls <= self.failures:
            raise RuntimeError("initial connection failed")
        self.initial_connection.set()


def install_sdk(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, [5, 10, 20, 40, 60, 60, 60]), (1.0, [10, 20, 40, 80, 120, 120, 120])],
)
async def test_initial_connection_retries_are_jittered_within_the_window_and_capped(
    monkeypatch: pytest.MonkeyPatch, value: float, expected: list[float]
) -> None:
    # Given an SDK client whose first seven connection attempts fail.
    client = FailingConnectWSClient(failures=7)
    install_sdk(monkeypatch, client)
    monkeypatch.setattr(wecom_stream, "_random_source", lambda: value)
    original_sleep = asyncio.sleep
    delays: list[float] = []

    async def advance_without_waiting(delay: float) -> None:
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(wecom_stream.asyncio, "sleep", advance_without_waiting)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()

    # When the stream client starts and retries until it connects.
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=1)

    # Then each wait came from the doubling window and stayed within the 5 to 120 second bounds.
    assert delays == expected
    assert manager.status() == {str(agent_id): True}
    await manager.stop_client(agent_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("parked_in", ["retry_wait", "connect"])
async def test_cancellation_during_an_initial_connection_retry_is_re_raised_at_once(
    monkeypatch: pytest.MonkeyPatch, parked_in: str
) -> None:
    # Given a client that is parked either in its retry wait or in the connection attempt itself.
    parked = asyncio.Event()

    async def park() -> None:
        parked.set()
        await asyncio.Event().wait()

    client = FailingConnectWSClient(failures=1_000, park=park if parked_in == "connect" else None)
    if parked_in == "retry_wait":
        monkeypatch.setattr(wecom_stream.asyncio, "sleep", lambda _delay: park())
    install_sdk(monkeypatch, client)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    task = manager._tasks[agent_id]
    await asyncio.wait_for(parked.wait(), timeout=1)

    # When the client is stopped.
    await manager.stop_client(agent_id)

    # Then the task ends cancelled without another connection attempt.
    assert task.cancelled()
    assert client.connect_calls == 1
    assert manager.status() == {}


@pytest.mark.asyncio
async def test_stale_stream_task_cannot_remove_or_disconnect_replacement_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: an old stream task whose runtime slots have been replaced
    old_client = FakeWSClient({})
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: old_client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(old_client.initial_connection.wait(), timeout=0.1)
    old_task = manager._tasks[agent_id]
    replacement_task = asyncio.create_task(wait_forever())
    replacement_client = ReplacementClient()
    manager._tasks[agent_id] = replacement_task
    manager._clients[agent_id] = replacement_client
    manager._connected[agent_id] = True

    # When: the stale task is cancelled and finishes its cleanup
    old_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old_task

    # Then: the replacement remains the sole owner of all runtime slots
    assert manager._tasks[agent_id] is replacement_task
    assert manager._clients[agent_id] is replacement_client
    assert manager._connected[agent_id] is True
    assert replacement_client.disconnect_calls == 0

    replacement_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement_task


@pytest.mark.asyncio
async def test_stream_task_failure_is_observed_and_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: stream setup that fails fatally before the SDK client is created
    logger = RecordingLogger()
    monkeypatch.setattr(wecom_stream, "logger", logger)

    def fail_setup() -> None:
        raise RuntimeError("fatal stream failure")

    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", fail_setup)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()

    # When: the manager starts the failing task
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    task = manager._tasks[agent_id]
    with pytest.raises(RuntimeError, match="fatal stream failure"):
        await task
    await asyncio.sleep(0)

    # Then: the done callback retrieves and logs the fatal failure
    assert logger.error_messages == [f"WeCom stream task failed for agent {agent_id}"]


@pytest.mark.asyncio
async def test_stream_task_cancellation_is_not_logged_as_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a live stream task that will be stopped
    logger = RecordingLogger()
    client = FakeWSClient({})
    monkeypatch.setattr(wecom_stream, "logger", logger)
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)

    # When: the stream task is stopped by its owner
    await manager.stop_client(agent_id)
    await asyncio.sleep(0)

    # Then: cancellation is observed without being logged as a failure
    assert logger.debug_messages == [f"WeCom stream task cancelled for agent {agent_id}"]
    assert not logger.error_messages


@pytest.mark.asyncio
async def test_reconcile_keeps_an_unchanged_live_client_and_records_its_health(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a configured WebSocket row and an SDK client that connects at once
    client = FakeWSClient({})
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    config = connection_config(monkeypatch, agent_id, "wecom")
    monkeypatch.setattr(wecom_stream.channel_config_dao, "list_configured", AsyncMock(return_value=[config]))
    recorded = asyncio.Event()
    set_connected = AsyncMock(side_effect=lambda *_args, **_kwargs: recorded.set())
    monkeypatch.setattr(wecom_stream.channel_config_dao, "set_connected", set_connected)

    # When: the connector pass starts the client
    await manager.reconcile_clients()
    await asyncio.wait_for(recorded.wait(), timeout=1)
    owner = manager._tasks[agent_id]

    try:
        # Then: the connection is recorded on the row for the API's status route
        assert set_connected.await_args_list == [call(config.id, connected=True)]
        # When: later passes read the same source and the recorded health
        config.is_connected = True
        await manager.reconcile_clients()
        await manager.reconcile_clients()
        # Then: the original task remains the sole client owner and nothing is rewritten
        assert manager._tasks[agent_id] is owner
        assert client.connect_calls == 1
        assert set_connected.await_count == 1
        # When: the client is stopped, Then: the row records it as disconnected
        await manager.stop_client(agent_id)
        assert set_connected.await_args_list[-1] == call(config.id, connected=False)
    finally:
        await manager.stop_client(agent_id)
        if not owner.done():
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner


@pytest.mark.asyncio
async def test_reconcile_corrects_a_stale_connected_flag_on_a_row_without_a_live_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a row left marked connected although it now asks for webhook mode
    manager = WeComStreamManager()
    config = connection_config(monkeypatch, uuid.uuid4(), "wecom")
    config.extra_config = {"connection_mode": "webhook"}
    config.is_connected = True
    monkeypatch.setattr(wecom_stream.channel_config_dao, "list_configured", AsyncMock(return_value=[config]))
    set_connected = AsyncMock()
    monkeypatch.setattr(wecom_stream.channel_config_dao, "set_connected", set_connected)

    # When: a connector pass runs
    await manager.reconcile_clients()

    # Then: no client starts and the stale flag is corrected
    assert manager.tracked_agents() == set()
    set_connected.assert_awaited_once_with(config.id, connected=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "image"])
@pytest.mark.parametrize("refusal", ["missing_id", "unlinked"])
async def test_stream_message_that_cannot_be_accepted_is_finished_with_an_empty_reply(
    monkeypatch: pytest.MonkeyPatch, kind: str, refusal: str
) -> None:
    # Given: a live client whose acceptance refuses a sender who is not a linked member
    client = FakeWSClient({})
    install_sdk(monkeypatch, client)
    reply = AsyncMock()
    monkeypatch.setattr(wecom_stream, "_reply_stream", reply)
    acceptance = AsyncMock(side_effect=ChannelUserResolutionError("Link your channel identity in the web application."))
    monkeypatch.setattr(submissions, "accept_channel_event", acceptance)
    monkeypatch.setattr(wecom_media, "is_channel_event_replay", acceptance)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    connection_config(monkeypatch, agent_id, "wecom")
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=1)
    detail: JsonObject = (
        {"content": "hello"}
        if kind == "text"
        else {"url": wecom_media.WECOM_STREAM_ORIGIN + "/signed", "aeskey": "key"}
    )
    body: JsonObject = {
        "msgtype": kind,
        "msgid": "" if refusal == "missing_id" else "native-1",
        "from_userid": "sender",
        kind: detail,
    }
    frame = SimpleNamespace(body=body)

    # When: the SDK delivers the message
    try:
        await client.handlers[f"message.{kind}"](frame)
    finally:
        await manager.stop_client(agent_id)

    # Then: the stream is finished without text, so the sender gets no error reply, and the message was not accepted
    reply.assert_awaited_once_with(client, frame, "stream", "")
    assert acceptance.await_count == (0 if refusal == "missing_id" else 1)
