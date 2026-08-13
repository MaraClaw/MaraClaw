import asyncio
import sys
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

import app.services.wecom_stream as wecom_stream
from app.core.json_types import JsonObject
from app.services.wecom_stream import (
    WeComStreamManager,
    _build_wecom_conv_id,
    _extract_wecom_chat_id,
    _extract_wecom_chat_type,
    _extract_wecom_sender_id,
)


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
        return

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
        return


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
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)

    # Then: the manager retries only the failed initial connection
    assert client.connect_calls == 2
    assert manager.status() == {str(agent_id): True}

    await manager.stop_client(agent_id)


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
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)

    # When: the stream task is stopped by its owner
    await manager.stop_client(agent_id)
    await asyncio.sleep(0)

    # Then: cancellation is observed without being logged as a failure
    assert logger.debug_messages == [f"WeCom stream task cancelled for agent {agent_id}"]
    assert not logger.error_messages


@pytest.mark.asyncio
async def test_non_stopping_start_retains_existing_live_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a live stream task for an agent
    client = FakeWSClient({})
    monkeypatch.setitem(
        sys.modules,
        "wecom_aibot_sdk",
        SimpleNamespace(WSClient=lambda _options: client, WSClientOptions=FakeWSClientOptions, generate_req_id=str),
    )
    monkeypatch.setattr(wecom_stream, "_disable_wecom_sdk_proxy", lambda: None)
    manager = WeComStreamManager()
    agent_id = uuid.uuid4()
    await manager.start_client(agent_id, "bot", "secret")
    await asyncio.wait_for(client.initial_connection.wait(), timeout=0.1)
    owner = manager._tasks[agent_id]

    try:
        # When: reconciliation asks to start the same agent without stopping it
        retained_task = await manager.start_client(agent_id, "bot", "secret", stop_existing=False)

        # Then: the original task remains the sole client owner
        assert retained_task is owner
        assert manager._tasks[agent_id] is owner
        assert client.connect_calls == 1
    finally:
        await manager.stop_client(agent_id)
        if not owner.done():
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
