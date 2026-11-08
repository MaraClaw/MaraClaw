"""Own transport because the SDK's start_forever retries cancellation indefinitely."""

from __future__ import annotations

import asyncio
import platform
import threading
from typing import TYPE_CHECKING, final
from urllib.parse import quote_plus

import httpx
from websockets.asyncio.client import connect

from app.core.json_types import JsonObject, json_as_str, json_loads_object, json_object_from_response
from app.runtime.tasks import OwnedTasks

if TYPE_CHECKING:
    import dingtalk_stream


@final
class DingTalkTransport:
    def __init__(self, stop_event: threading.Event) -> None:
        self._stop_event = stop_event
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[object] | None = None

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._loop is not None and self._task is not None:
                _ = self._loop.call_soon_threadsafe(self._task.cancel)

    def run(self, client: dingtalk_stream.DingTalkStreamClient) -> None:
        asyncio.run(self._run(client))

    async def _run(self, client: dingtalk_stream.DingTalkStreamClient) -> None:
        from dingtalk_stream.version import VERSION_STRING

        with self._lock:
            if self._stop_event.is_set():
                return
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        try:
            client.pre_start()
            subscriptions = [{"type": "CALLBACK", "topic": topic} for topic in client.callback_handler_map]
            if client._is_event_required:
                subscriptions.append({"type": "EVENT", "topic": "*"})
            headers = {
                "Accept": "application/json",
                "User-Agent": f"DingTalkStream/1.0 SDK/{VERSION_STRING} Python/{platform.python_version()} "
                "(+https://github.com/open-dingtalk/dingtalk-stream-sdk-python)",
            }
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            ) as http:
                response = await http.post(
                    client.OPEN_CONNECTION_API,
                    headers=headers,
                    json={
                        "clientId": client.credential.client_id,
                        "clientSecret": client.credential.client_secret,
                        "subscriptions": subscriptions,
                        "ua": f"dingtalk-sdk-python/v{VERSION_STRING}-union",
                        "localIp": client.get_host_ip(),
                    },
                )
                _ = response.raise_for_status()
                connection = json_object_from_response(response)
            endpoint, ticket = json_as_str(connection.get("endpoint")), json_as_str(connection.get("ticket"))
            if endpoint is None or ticket is None:
                raise ValueError("DingTalk connection response requires endpoint and ticket")
            async with connect(f"{endpoint}?ticket={quote_plus(ticket)}", close_timeout=2.0) as websocket:
                client.websocket = websocket
                children = OwnedTasks()
                try:
                    children.start("dingtalk-keepalive", client.keepalive(websocket))
                    async for raw in websocket:
                        payload: JsonObject = json_loads_object(raw)
                        children.start("dingtalk-frame", client.background_task(payload))
                finally:
                    await websocket.close()
                    await children.stop()
        except asyncio.CancelledError:
            if not self._stop_event.is_set():
                raise
        finally:
            with self._lock:
                self._task = None
                self._loop = None
