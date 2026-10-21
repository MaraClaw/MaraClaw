"""WeCom AI Bot WebSocket Long Connection Manager.

Uses the wecom-aibot-sdk-python SDK for WebSocket-based message reception.
No callback URL or domain verification needed.
"""

import asyncio
import inspect
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import TypeIs, cast
from unittest.mock import patch

from app.core.json_types import JsonObject, JsonValue
from app.core.logging import logger
from app.runtime.tasks import EventTasks

_WECOM_SDK_PROXY_DISABLED = False

# SDK WsFrame is too narrow for tests/mocks; keep these names for call sites.
type _WeComFrame = object
type _WeComClient = object


def _json_object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        result[key] = item if _is_json_value(item) else None
    return result


def _is_json_value(value: object) -> TypeIs[JsonValue]:
    if value is None or isinstance(value, str | int | float | bool):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _disable_wecom_sdk_proxy() -> None:
    """Force the WeCom SDK websocket path to bypass system proxies."""
    global _WECOM_SDK_PROXY_DISABLED

    import wecom_aibot_sdk.ws as sdk_ws

    if _WECOM_SDK_PROXY_DISABLED:
        return

    original_connect = sdk_ws.websockets.connect

    def without_proxy[**P, R](original: Callable[P, R]) -> Callable[P, R]:
        def connect_no_proxy(*args: P.args, **kwargs: P.kwargs) -> R:
            _ = cast(dict[str, object], kwargs).setdefault("proxy", None)
            return original(*args, **kwargs)

        return connect_no_proxy

    _ = patch.object(sdk_ws.websockets, "connect", without_proxy(original_connect)).start()
    _WECOM_SDK_PROXY_DISABLED = True


async def _await_maybe(value: object) -> object:
    if not asyncio.iscoroutine(value):
        return value
    from collections.abc import Awaitable
    from typing import cast

    return await cast(Awaitable[object], value)


async def _reply_stream(client: object, frame: object, stream_id: str, content: str) -> None:
    reply = getattr(client, "reply_stream", None)
    if callable(reply):
        _ = await _await_maybe(reply(frame, stream_id, content, finish=True))


async def _reply_welcome(client: object, frame: object, message: JsonObject) -> None:
    reply = getattr(client, "reply_welcome", None)
    if callable(reply):
        _ = await _await_maybe(reply(frame, message))


async def _disconnect_client(client: object) -> None:
    disconnect = getattr(client, "disconnect", None)
    if callable(disconnect):
        if inspect.iscoroutinefunction(disconnect):
            _ = await _await_maybe(disconnect())
        else:
            try:
                _ = await _await_maybe(await asyncio.wait_for(asyncio.to_thread(disconnect), timeout=5))
            except TimeoutError:
                logger.warning("[WeCom Stream] SDK disconnect thread exceeded 5s; thread may still be running")


def _extract_wecom_sender_id(body: JsonObject) -> str:
    sender = body.get("from")
    if isinstance(sender, dict):
        sender_id = sender.get("user_id") or sender.get("userid")
        if sender_id is not None and sender_id:
            return str(sender_id).strip()
    return str(body.get("from_userid") or body.get("userid") or "").strip()


def _extract_wecom_chat_type(body: JsonObject) -> str:
    return str(body.get("chattype") or body.get("chat_type") or "single").strip().lower()


def _extract_wecom_chat_id(body: JsonObject) -> str:
    return str(body.get("chatid") or body.get("chat_id") or "").strip()


def _build_wecom_conv_id(sender_id: str, chat_id: str, chat_type: str) -> str:
    normalized_type = (chat_type or "single").strip().lower()
    if normalized_type in {"group", "groupchat", "group_chat"} and chat_id:
        return f"wecom_group_{chat_id}"
    return f"wecom_p2p_{sender_id}"


class WeComStreamManager:
    """Manages WeCom AI Bot WebSocket clients for all agents."""

    def __init__(self):
        self._clients: dict[uuid.UUID, _WeComClient] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._connected: dict[uuid.UUID, bool] = {}
        self._event_tasks: EventTasks = EventTasks()

    async def start_client(
        self,
        agent_id: uuid.UUID,
        bot_id: str,
        bot_secret: str,
        stop_existing: bool = True,
    ) -> asyncio.Task[None] | None:
        """Start a WeCom AI Bot WebSocket client for a specific agent."""
        if not bot_id or not bot_secret:
            logger.warning(f"[WeCom Stream] Missing bot_id or bot_secret for {agent_id}, skipping")
            return None

        logger.info(f"[WeCom Stream] Starting client for agent {agent_id} (BotID: {bot_id[:12]}...)")

        # Stop existing client if any
        if stop_existing:
            await self.stop_client(agent_id)
        else:
            existing_task = self._tasks.get(agent_id)
            if existing_task is not None and not existing_task.done():
                return existing_task

        self._connected[agent_id] = False
        self._event_tasks.closing = False
        task = asyncio.create_task(
            self._run_client(agent_id, bot_id, bot_secret),
            name=f"wecom-stream-{str(agent_id)[:8]}",
        )
        self._tasks[agent_id] = task

        def observe_completion(completed_task: asyncio.Task[None]) -> None:
            if completed_task.cancelled():
                logger.debug(f"WeCom stream task cancelled for agent {agent_id}")
                return
            if error := completed_task.exception():
                logger.opt(exception=error).error(f"WeCom stream task failed for agent {agent_id}")

        task.add_done_callback(observe_completion)
        return task

    async def _run_client(
        self,
        agent_id: uuid.UUID,
        bot_id: str,
        bot_secret: str,
    ) -> None:
        """Run the WeCom WebSocket client (async, runs in the main event loop)."""
        task = asyncio.current_task()
        client: _WeComClient | None = None
        try:
            from wecom_aibot_sdk import WSClient, WSClientOptions, generate_req_id
        except ImportError:
            self._connected[agent_id] = False
            logger.warning(
                "[WeCom Stream] wecom-aibot-sdk-python not installed. Install with: pip install wecom-aibot-sdk-python"
            )
            return

        try:
            _disable_wecom_sdk_proxy()
            stream_client = WSClient(
                WSClientOptions(
                    bot_id=bot_id,
                    secret=bot_secret,
                    max_reconnect_attempts=-1,
                    heartbeat_interval=30000,
                )
            )
            client = stream_client
            self._clients[agent_id] = stream_client

            async def on_disconnected(_reason: object = None) -> None:
                if self._clients.get(agent_id) is stream_client:
                    self._connected[agent_id] = False

            # ── Message handler: text ──
            async def on_text(frame: _WeComFrame) -> None:
                try:
                    body = _json_object(getattr(frame, "body", None))
                    text_obj = _json_object(body.get("text"))
                    if not text_obj:
                        return
                    content = text_obj.get("content")
                    if not isinstance(content, str):
                        return
                    user_text = content.strip()
                    if not user_text:
                        return

                    sender_id = _extract_wecom_sender_id(body)
                    if not sender_id:
                        logger.warning(
                            f"[WeCom Stream] Missing sender id in text payload for agent {agent_id}: "
                            + f"body_keys={list(body.keys())}"
                        )
                        stream_id = generate_req_id("stream")
                        await _reply_stream(
                            stream_client,
                            frame,
                            stream_id,
                            "Unable to identify the sender for this WeCom message.",
                        )
                        return

                    chat_type = _extract_wecom_chat_type(body)
                    chat_id = _extract_wecom_chat_id(body)
                    is_group_msg = chat_type in {"group", "groupchat", "group_chat"} and bool(chat_id)

                    # Debug: log full body to understand the data structure
                    logger.info(
                        f"[WeCom Stream] Text from {sender_id}, "
                        + f"chat_type={chat_type}, is_group={is_group_msg}, chat_id={chat_id or 'N/A'}, "
                        + f"body_keys={list(body.keys())}: {user_text[:80]}"
                    )

                    # Process message and get reply
                    reply_text = await _process_wecom_stream_message(
                        agent_id=agent_id,
                        sender_id=sender_id,
                        user_text=user_text,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        message_id=str(body.get("msgid") or body.get("msg_id") or ""),
                    )

                    # Reply via streaming
                    stream_id = generate_req_id("stream")
                    await _reply_stream(stream_client, frame, stream_id, reply_text)
                    logger.info(f"[WeCom Stream] Replied to {sender_id}: {reply_text[:80]}")

                except Exception as e:
                    logger.error(f"[WeCom Stream] Error handling text message: {e}")
                    import traceback

                    traceback.print_exc()
                    try:
                        stream_id = generate_req_id("stream")
                        await _reply_stream(
                            stream_client,
                            frame,
                            stream_id,
                            f"Processing error: {str(e)[:100]}",
                        )
                    except Exception as reply_error:
                        logger.warning(f"[WeCom Stream] Could not send error reply for {agent_id}: {reply_error}")

            # ── Message handler: image ──
            async def on_image(frame: _WeComFrame) -> None:
                try:
                    body = _json_object(getattr(frame, "body", None))
                    sender_id = _extract_wecom_sender_id(body)
                    logger.info(f"[WeCom Stream] Image message from {sender_id} (not yet handled)")
                    stream_id = generate_req_id("stream")
                    await _reply_stream(
                        stream_client,
                        frame,
                        stream_id,
                        "Received your image. Image processing is not yet supported.",
                    )
                except Exception as e:
                    logger.error(f"[WeCom Stream] Error handling image: {e}")

            # ── Message handler: file ──
            async def on_file(frame: _WeComFrame) -> None:
                try:
                    body = _json_object(getattr(frame, "body", None))
                    sender_id = _extract_wecom_sender_id(body)
                    logger.info(f"[WeCom Stream] File message from {sender_id} (not yet handled)")
                    stream_id = generate_req_id("stream")
                    await _reply_stream(
                        stream_client,
                        frame,
                        stream_id,
                        "Received your file. File processing is not yet supported.",
                    )
                except Exception as e:
                    logger.error(f"[WeCom Stream] Error handling file: {e}")

            # ── Enter chat event: send welcome ──
            async def on_enter_chat(frame: _WeComFrame) -> None:
                try:
                    # Look up agent's welcome message

                    from app.dao import agent_dao as _agent_dao

                    agent = await _agent_dao.get(agent_id)
                    welcome = (agent.welcome_message if agent else None) or "Hello! How can I help you?"
                    welcome_message: JsonObject = {
                        "msgtype": "text",
                        "text": {"content": welcome},
                    }
                    await _reply_welcome(stream_client, frame, welcome_message)
                    logger.info(f"[WeCom Stream] Sent welcome message for agent {agent_id}")
                except Exception as e:
                    logger.error(f"[WeCom Stream] Error sending welcome: {e}")

            # Register event handlers
            stream_client.on("message.text", self._event_tasks.wrap(on_text))
            stream_client.on("message.image", self._event_tasks.wrap(on_image))
            stream_client.on("message.file", self._event_tasks.wrap(on_file))
            stream_client.on("event.enter_chat", self._event_tasks.wrap(on_enter_chat))
            stream_client.on("disconnected", on_disconnected)

            # The SDK handles reconnects after a successful connection.
            retry_delay = 5  # Start with 5 seconds
            max_retry_delay = 120  # Cap at 2 minutes
            while True:
                try:
                    logger.info(f"[WeCom Stream] Connecting for agent {agent_id}...")
                    await stream_client.connect_async()
                    if self._clients.get(agent_id) is stream_client:
                        self._connected[agent_id] = True
                    break
                except asyncio.CancelledError:
                    raise  # Propagate cancellation
                except Exception as e:
                    if self._clients.get(agent_id) is stream_client:
                        self._connected[agent_id] = False
                    logger.error(f"[WeCom Stream] Connection error for {agent_id}: {e}, retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, max_retry_delay)

            await asyncio.Future[None]()

        except asyncio.CancelledError:
            if client is not None and self._clients.get(agent_id) is client:
                self._connected[agent_id] = False
            logger.info(f"[WeCom Stream] Client task cancelled for agent {agent_id}")
            if client is not None:
                try:
                    await _disconnect_client(client)
                except Exception as disconnect_error:
                    logger.warning(
                        f"[WeCom Stream] Disconnect during cancellation failed for {agent_id}: {disconnect_error}"
                    )
            raise
        finally:
            if self._tasks.get(agent_id) is task:
                _ = self._tasks.pop(agent_id, None)
                _ = self._connected.pop(agent_id, None)
            if self._clients.get(agent_id) is client:
                _ = self._clients.pop(agent_id, None)

    async def stop_client(self, agent_id: uuid.UUID) -> None:
        """Stop a running WebSocket client for an agent."""
        task = self._tasks.pop(agent_id, None)
        client = self._clients.pop(agent_id, None)
        _ = self._connected.pop(agent_id, None)
        if task and not task.done():
            _ = task.cancel()
            logger.info(f"[WeCom Stream] Stopped client for agent {agent_id}")
            with suppress(asyncio.CancelledError):
                await task
        elif client:
            try:
                await _disconnect_client(client)
            except Exception as disconnect_error:
                logger.warning(f"[WeCom Stream] Disconnect failed for {agent_id}: {disconnect_error}")

    async def start_all(self) -> None:
        """Start WebSocket clients for all configured WeCom agents with bot credentials."""
        logger.info("[WeCom Stream] Initializing all active WeCom AI Bot channels...")
        from app.dao.channel_config_dao import channel_config_dao

        configs = await channel_config_dao.list_configured("wecom")

        started = 0
        for config in configs:
            extra = _json_object(config.extra_config)
            bot_id = extra.get("bot_id", "")
            bot_secret = extra.get("bot_secret", "")
            if isinstance(bot_id, str) and isinstance(bot_secret, str) and bot_id and bot_secret:
                _ = await self.start_client(
                    config.agent_id,
                    bot_id,
                    bot_secret,
                    stop_existing=False,
                )
                started += 1

        logger.info(f"[WeCom Stream] Started {started} WeCom AI Bot client(s)")

    async def stop_all(self) -> None:
        self._event_tasks.closing = True
        results = await asyncio.gather(
            *(self.stop_client(agent_id) for agent_id in set(self._tasks) | set(self._clients)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("[WeCom Stream] Client cleanup failed: {}", result)
        await self._event_tasks.stop()
        self._connected.clear()

    def status(self) -> dict[str, bool]:
        """Return status of all active WebSocket clients."""
        return {str(aid): connected for aid, connected in self._connected.items()}


# ── Message processing helper ──


async def _process_wecom_stream_message(
    agent_id: uuid.UUID,
    sender_id: str,
    user_text: str,
    chat_id: str = "",
    chat_type: str = "single",
    message_id: str = "",
) -> str:
    from app.dao.channel_config_dao import channel_config_dao
    from app.services.channels.submissions import ChannelEvent, accept_channel_event

    if not message_id:
        raise ValueError("WeCom message ID is required")
    config = await channel_config_dao.get_for_agent(agent_id=agent_id, channel_type="wecom")
    if config is None:
        raise RuntimeError("WeCom channel configuration unavailable")
    await accept_channel_event(
        ChannelEvent(
            config_id=config.id,
            agent_id=agent_id,
            provider="wecom",
            event_key=f"{config.app_id}:{chat_id}:{message_id}",
            sender_id=sender_id,
            conversation_id=_build_wecom_conv_id(sender_id, chat_id, chat_type),
            text=user_text,
            is_group=chat_type in {"group", "groupchat", "group_chat"} and bool(chat_id),
        )
    )
    return ""


wecom_stream_manager = WeComStreamManager()
