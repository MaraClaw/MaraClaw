"""Feishu WebSocket Long Connection Manager."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol, TypeIs
from unittest.mock import patch

from app.core.json_types import JsonObject, json_as_str_or, json_loads_object, json_object_from, mapping_from_row
from app.core.logging import logger
from app.dao.channel_config_dao import channel_config_dao
from app.runtime.tasks import EventTasks, cancel_tasks, is_async_callback

try:
    import lark_oapi as _lark_mod
    import lark_oapi.ws as _lark_ws_mod

    _HAS_LARK = True
except ImportError:
    _lark_mod = None
    _lark_ws_mod = None
    _HAS_LARK = False


def _is_json_object(value: object) -> TypeIs[JsonObject]:
    return isinstance(value, dict)


def _json_object(value: object) -> JsonObject:
    return value if _is_json_object(value) else mapping_from_row(value)


class LarkWSClient(Protocol):
    async def _connect(self) -> None: ...

    async def _ping_loop(self) -> None: ...

    async def _disconnect(self) -> None: ...


class _EventHandler(Protocol):
    pass


class _EventHandlerBuilder(Protocol):
    def register_p2_customized_event(self, name: str, handler: Callable[[object], None]) -> _EventHandlerBuilder: ...

    def build(self) -> _EventHandler: ...


def _is_lark_ws_client(value: object) -> TypeIs[LarkWSClient]:
    return hasattr(value, "_connect") and hasattr(value, "_disconnect")


def _is_event_builder(value: object) -> TypeIs[_EventHandlerBuilder]:
    return callable(getattr(value, "register_p2_customized_event", None)) and callable(getattr(value, "build", None))


def _is_awaitable(value: object) -> TypeIs[Awaitable[object]]:
    return hasattr(value, "__await__")


def _construct_ws_client(client_cls: object, *args: object, **kwargs: object) -> object:
    if not callable(client_cls):
        raise TypeError("lark-oapi ws Client is unavailable")
    return client_cls(*args, **kwargs)


def _header_mapping(header_obj: object) -> JsonObject:
    if hasattr(header_obj, "__dict__"):
        return _json_object(vars(header_obj))
    return {
        "event_type": str(getattr(header_obj, "event_type", "im.message.receive_v1")),
        "event_id": str(getattr(header_obj, "event_id", "")),
        "create_time": str(getattr(header_obj, "create_time", "")),
    }


def _feishu_event_body(data: object) -> JsonObject | None:
    """Normalize a Feishu WS event payload into a JSON object, or None if unusable."""
    raw_body: object = getattr(data, "raw_body", None)
    if not raw_body:
        if isinstance(data, dict):
            return _json_object(data)
        body_dict: JsonObject = {}
        if hasattr(data, "header"):
            header_obj: object = getattr(data, "header", None)
            header = _header_mapping(header_obj)
            if "event_type" not in header:
                header["event_type"] = "im.message.receive_v1"
            body_dict["header"] = header
        else:
            body_dict["header"] = {"event_type": "im.message.receive_v1"}

        if hasattr(data, "event"):
            event_raw: object = getattr(data, "event", None)
            body_dict["event"] = _json_object(event_raw) if isinstance(event_raw, dict) else mapping_from_row(event_raw)
        else:
            content_raw: object = getattr(data, "content", None)
            if isinstance(content_raw, str):
                try:
                    loaded = json_loads_object(content_raw)
                    body_dict["event"] = loaded if loaded else {"content": content_raw}
                except json.JSONDecodeError:
                    body_dict["event"] = {"content": content_raw}

        if not hasattr(data, "header") and not hasattr(data, "event"):
            return None
        return body_dict

    if isinstance(raw_body, (bytes, bytearray)):
        return json_loads_object(bytes(raw_body))
    decode = getattr(raw_body, "decode", None)
    if callable(decode):
        text_obj: object = decode("utf-8")
        text = text_obj if isinstance(text_obj, str) else str(text_obj)
        return json_loads_object(text)
    return {}


if _HAS_LARK:
    try:
        import websockets as _websockets

        # Keep a reference to the original connect so we can restore it if needed.
        _orig_websockets_connect = _websockets.connect
        _PROXY_PATCH_AVAILABLE = True
    except ImportError:
        _PROXY_PATCH_AVAILABLE = False
else:
    _PROXY_PATCH_AVAILABLE = False


def _make_no_proxy_connect(orig_connect: Callable[..., object]) -> Callable[[], AbstractAsyncContextManager[None]]:
    """Return a drop-in replacement for websockets.connect that forces proxy=None.

    This is intentionally NOT applied at module import time to avoid polluting
    the global websockets namespace for other modules in the process.  Instead
    it is applied as a scoped context manager around lark-oapi's _connect() call.
    """
    import contextlib

    class _NoProxyConnect:
        """Wraps websockets.connect to inject proxy=None, preventing macOS
        system-proxy interference with long-lived SSE / WebSocket connections."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = kwargs.setdefault("proxy", None)
            started: object = orig_connect(*args, **kwargs)
            if not _is_awaitable(started):
                raise TypeError("websockets.connect did not return an awaitable")
            self._coro: Awaitable[object] = started
            self._ws: object = None

        def __await__(self) -> object:
            return self._coro.__await__()

        async def __aenter__(self) -> object:
            self._ws = await self._coro
            return self._ws

        async def __aexit__(self, *exc: object) -> None:
            closer = getattr(self._ws, "close", None)
            if callable(closer):
                closed: object = closer()
                if _is_awaitable(closed):
                    await closed

    @contextlib.asynccontextmanager
    async def _scoped_no_proxy() -> AsyncIterator[None]:
        """Context manager that temporarily replaces websockets.connect for
        the duration of the lark-oapi connection handshake only."""
        if not _PROXY_PATCH_AVAILABLE:
            yield
            return
        import websockets

        with patch.object(websockets, "connect", _NoProxyConnect):
            logger.debug("[Feishu WS] Scoped websockets proxy bypass: active")
            yield
            logger.debug("[Feishu WS] Scoped websockets proxy bypass: restored")

    return _scoped_no_proxy


if not _HAS_LARK:
    logger.warning(
        "[Feishu WS] lark-oapi package not installed. "
        + "Feishu WebSocket features will be disabled. "
        + "Install with: pip install lark-oapi"
    )


class FeishuWSManager:
    """Manages Feishu WebSocket clients for all agents."""

    def __init__(self) -> None:
        self._clients: dict[uuid.UUID, LarkWSClient] = {}
        # Tasks for reconnection or ping loops if we want to cancel them later
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._ping_tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._sdk_tasks: EventTasks = EventTasks()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._closing: bool = False

    def _create_event_handler(self, agent_id: uuid.UUID) -> _EventHandler:
        """Create an event dispatcher for a specific agent."""

        def handle_message(data: object) -> None:
            """Handle im.message.receive_v1 events from Feishu WebSocket."""
            try:
                logger.info(f"[Feishu WS] Received event: {data}")
                if _feishu_event_body(data) is None:
                    logger.warning(f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}")
                    return

                def dispatch() -> None:
                    if self._closing:
                        return
                    task = asyncio.create_task(self._async_handle_message(agent_id, data))
                    self._event_tasks.add(task)
                    task.add_done_callback(self._event_tasks.discard)

                if self._main_loop and self._main_loop.is_running():
                    self._main_loop.call_soon_threadsafe(dispatch)
            except RuntimeError as exc:
                logger.warning("[Feishu WS] Could not dispatch event: {}", exc)

        if _lark_mod is None:
            raise RuntimeError("lark-oapi package is not installed")
        handler_cls: object = getattr(_lark_mod, "EventDispatcherHandler", None)
        builder_fn: object = getattr(handler_cls, "builder", None)
        if not callable(builder_fn):
            raise RuntimeError("lark-oapi EventDispatcherHandler.builder is unavailable")
        started: object = builder_fn("", "")
        if not _is_event_builder(started):
            raise RuntimeError("lark-oapi EventDispatcherHandler.builder returned an unexpected object")
        return started.register_p2_customized_event("im.message.receive_v1", handle_message).build()

    async def _async_handle_message(self, agent_id: uuid.UUID, data: object) -> None:
        """Handle im.message.receive_v1 events from Feishu WebSocket asynchronously."""
        try:
            body_dict = _feishu_event_body(data)
            if body_dict is None:
                logger.warning(f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}")
                return

            event_type = json_as_str_or(_json_object(body_dict.get("header")).get("event_type"), "unknown")
            logger.info(f"[Feishu WS] Event received for agent {agent_id}: {event_type}")

            # Import here to avoid circular dependencies
            from app.api.feishu import process_feishu_event

            _handled: JsonObject = await process_feishu_event(agent_id, body_dict)
            del _handled

        except Exception as e:
            logger.exception(f"[Feishu WS] Error processing event for {agent_id}: {e}")

    async def start_client(
        self,
        agent_id: uuid.UUID,
        app_id: str,
        app_secret: str,
        stop_existing: bool = True,
    ) -> None:
        """Spawns a WebSocket client fully asynchronously inside FastAPI's loop."""
        if not _HAS_LARK:
            logger.warning("[Feishu WS] lark-oapi not installed, cannot start client")
            return

        # Monkeypatch lark-oapi global event loop to use the current running event loop.
        # This is critical because lark-oapi initializes 'loop = asyncio.get_event_loop()'
        # at module import time, which refers to a dead loop in FastAPI/Uvicorn processes.
        try:
            import lark_oapi.ws.client as lark_ws_client

            lark_ws_client.loop = asyncio.get_running_loop()
            logger.debug("[Feishu WS] Patched lark_oapi.ws.client.loop with running loop")
        except Exception as e:
            logger.warning(f"[Feishu WS] Failed to patch lark-oapi event loop: {e}")
        if not app_id or not app_secret:
            logger.warning(f"[Feishu WS] Missing app_id or app_secret for {agent_id}, skipping")
            return

        logger.info(f"[Feishu WS] Starting async WS client for agent {agent_id} (App ID: {app_id})")

        self._main_loop = asyncio.get_running_loop()
        self._closing = False
        self._sdk_tasks.closing = False
        if stop_existing:
            await self.stop_client(agent_id)
        elif agent_id in self._tasks and not self._tasks[agent_id].done():
            return

        try:
            event_handler = self._create_event_handler(agent_id)
        except Exception as e:
            logger.exception(f"[Feishu WS] Failed to create event handler for {agent_id}: {e}")
            return

        # Instantiate Client - SDK manages connect + receive + ping internally.
        # We set auto_reconnect=True so the SDK handles reconnections.
        if _lark_ws_mod is None or _lark_mod is None:
            logger.error("[Feishu WS] lark-oapi package is not installed")
            return
        client_cls: object = getattr(_lark_ws_mod, "Client", None)
        if not callable(client_cls):
            logger.error("[Feishu WS] lark-oapi ws Client is unavailable")
            return
        log_level_info: object = getattr(getattr(_lark_mod, "LogLevel", None), "INFO", None)
        built = _construct_ws_client(
            client_cls,
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=log_level_info,
            auto_reconnect=True,
        )
        if not _is_lark_ws_client(built):
            logger.error("[Feishu WS] lark-oapi ws Client constructor returned an unexpected object")
            return
        client = built
        self._clients[agent_id] = client
        # Retain SDK-spawned receive/message work for this client, without changing SDK retries.
        for name in ("_receive_message_loop", "_handle_message"):
            callback: object = getattr(client, name, None)
            if is_async_callback(callback):
                setattr(client, name, self._sdk_tasks.wrap(callback))

        # Build scoped proxy bypass: active only during _connect() to avoid
        # permanently replacing websockets.connect for the whole process.
        _no_proxy_ctx = _make_no_proxy_connect(_orig_websockets_connect) if _PROXY_PATCH_AVAILABLE else None

        async def _do_full_connect() -> None:
            """Perform a single clean connect + start receive/ping loops.

            This is the ONLY place we call _connect() and _ping_loop().
            The SDK's internal _reconnect() will handle subsequent reconnections.
            """
            if _no_proxy_ctx:
                async with _no_proxy_ctx():
                    await client._connect()
            else:
                await client._connect()
            self._ping_tasks[agent_id] = asyncio.create_task(client._ping_loop())

        async def _run_async_client() -> None:
            try:
                logger.info(f"[Feishu WS] Connecting for agent {agent_id}")
                await _do_full_connect()
                logger.info(f"[Feishu WS] Connected for agent {agent_id}, receive loop started")
            except asyncio.CancelledError:
                await client._disconnect()
                return
            except Exception as e:
                logger.exception(f"[Feishu WS] Initial connect failed for agent {agent_id}: {e}")

            # Health-watch: only log status changes for diagnostics.
            # SDK handles reconnect internally via _receive_message_loop → _reconnect.
            # We do NOT call _connect() or _ping_loop() again to avoid creating
            # duplicate connections that cause "kicked by new connection".
            _last_conn_id: object = getattr(client, "_conn_id", None)
            _was_disconnected = False
            while True:
                try:
                    await asyncio.sleep(30)  # Check every 30 seconds

                    conn: object = getattr(client, "_conn", None)
                    curr_conn_id: object = getattr(client, "_conn_id", None)

                    if conn is None:
                        if not _was_disconnected:
                            logger.warning(
                                f"[Feishu WS] Connection lost for agent {agent_id} "
                                + f"(last conn_id={_last_conn_id}), "
                                + "waiting for SDK auto-reconnect..."
                            )
                            _was_disconnected = True
                    elif bool(getattr(conn, "closed", False)):
                        if not _was_disconnected:
                            logger.warning(
                                f"[Feishu WS] WebSocket closed for agent {agent_id}, waiting for SDK auto-reconnect..."
                            )
                            _was_disconnected = True
                    else:
                        if _was_disconnected:
                            logger.info(
                                f"[Feishu WS] Connection restored for agent {agent_id} (new conn_id={curr_conn_id})"
                            )
                            _was_disconnected = False
                        if curr_conn_id != _last_conn_id and curr_conn_id:
                            logger.info(
                                f"[Feishu WS] Connection ID changed for agent {agent_id}: "
                                + f"{_last_conn_id} → {curr_conn_id}"
                            )
                            _last_conn_id = curr_conn_id
                except asyncio.CancelledError:
                    logger.info(f"[Feishu WS] Task cancelled for agent {agent_id}")
                    try:
                        await client._disconnect()
                    except Exception as error:
                        logger.debug(f"[Feishu WS] Disconnect during cancellation failed: {error}")
                    return
                except Exception as e:
                    logger.exception(f"[Feishu WS] Health-watch error for agent {agent_id}: {e}")

        task = asyncio.create_task(_run_async_client(), name=f"feishu-ws-async-{str(agent_id)[:8]}")
        self._tasks[agent_id] = task
        logger.info(f"[Feishu WS] Async WS task scheduled for agent {agent_id}")

    async def stop_client(self, agent_id: uuid.UUID) -> None:
        """Stops an actively running WebSocket client for an agent."""
        ping_task = self._ping_tasks.pop(agent_id, None)
        task = self._tasks.pop(agent_id, None)
        await cancel_tasks(child for child in (ping_task, task) if child is not None)
        if agent_id in self._clients:
            client = self._clients.pop(agent_id)
            try:
                await client._disconnect()
            except Exception as e:
                logger.error(f"[Feishu WS] Error disconnecting client for {agent_id}: {e}")

    async def stop_all(self) -> None:
        self._closing = True
        self._sdk_tasks.closing = True
        await self._sdk_tasks.stop()
        results = await asyncio.gather(
            *(self.stop_client(agent_id) for agent_id in set(self._tasks) | set(self._clients) | set(self._ping_tasks)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("[Feishu WS] Client cleanup failed: {}", result)
        await cancel_tasks(self._event_tasks)
        self._event_tasks.clear()
        self._main_loop = None

    async def start_all(self) -> None:
        """Start WS clients for all configured Feishu agents."""
        if not _HAS_LARK:
            logger.info("[Feishu WS] lark-oapi not installed, skipping Feishu WS initialization")
            return
        logger.info("[Feishu WS] Initializing all active Feishu channels...")
        configs = await channel_config_dao.list_configured("feishu")

        for config in configs:
            extra = json_object_from(config.extra_config)
            mode = json_as_str_or(extra.get("connection_mode"), "webhook")
            if mode == "websocket":
                if config.app_id and config.app_secret:
                    await self.start_client(config.agent_id, config.app_id, config.app_secret, stop_existing=False)
                else:
                    logger.warning(f"[Feishu WS] Skipping agent {config.agent_id}: missing credentials")

    def status(self) -> dict[str, bool]:
        """Return status of all active WS tasks."""
        return {str(aid): not self._tasks[aid].done() for aid in self._tasks}


feishu_ws_manager = FeishuWSManager()
