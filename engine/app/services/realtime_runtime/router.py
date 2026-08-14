"""Redis-backed websocket presence and cross-instance message routing."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import assert_never

from fastapi import WebSocket
from redis.asyncio import Redis

from app.config import get_settings
from app.core.events import get_redis
from app.core.json_types import json_loads_object, json_object_from
from app.core.logging import logger

settings = get_settings()

PRESENCE_TTL_SECONDS = 180
PUBSUB_PREFIX = "realtime:ws"

type LocalDeliverer = Callable[..., Awaitable[object]]


class RealtimeRouter:
    def __init__(self) -> None:
        self.instance_id: str = settings.INSTANCE_ID
        self._subscriber_task: asyncio.Task[None] | None = None
        self._started: bool = False

    def _connection_key(self, connection_id: str) -> str:
        return f"{PUBSUB_PREFIX}:conn:{connection_id}"

    def _agent_index_key(self, agent_id: str) -> str:
        return f"{PUBSUB_PREFIX}:agent:{agent_id}"

    def _instance_channel(self) -> str:
        return f"{PUBSUB_PREFIX}:instance:{self.instance_id}"

    async def register_connection(
        self,
        *,
        agent_id: str,
        websocket: WebSocket,
        session_id: str | None,
        user_id: str | None,
    ) -> str:
        connection_id = uuid.uuid4().hex
        redis_client: Redis = await get_redis()
        payload = {
            "agent_id": agent_id,
            "session_id": session_id or "",
            "user_id": user_id or "",
            "instance_id": self.instance_id,
        }
        async with redis_client.pipeline(transaction=True) as pipe:
            _ = pipe.sadd(self._agent_index_key(agent_id), connection_id)
            connection_key = self._connection_key(connection_id)
            for field, value in payload.items():
                _ = pipe.hset(connection_key, field, value)
            _ = pipe.expire(self._connection_key(connection_id), PRESENCE_TTL_SECONDS)
            _ = pipe.expire(self._agent_index_key(agent_id), PRESENCE_TTL_SECONDS)
            _ = await pipe.execute()
        websocket.state.realtime_connection_id = connection_id
        return connection_id

    async def unregister_connection(self, *, agent_id: str, websocket: WebSocket) -> None:
        connection_id_raw = getattr(websocket.state, "realtime_connection_id", None)
        if not isinstance(connection_id_raw, str) or not connection_id_raw:
            return
        connection_id = connection_id_raw
        redis = await get_redis()
        async with redis.pipeline(transaction=True) as pipe:
            _ = pipe.srem(self._agent_index_key(agent_id), connection_id)
            _ = pipe.delete(self._connection_key(connection_id))
            _ = await pipe.execute()

    async def is_user_viewing_session(self, *, agent_id: str, session_id: str, user_id: str) -> bool:
        for record in await self._list_presence(agent_id):
            if record.get("session_id") == session_id and record.get("user_id") == user_id:
                return True
        return False

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        seen: set[str] = set()
        for record in await self._list_presence(agent_id):
            session_id = (record.get("session_id") or "").strip()
            if session_id:
                seen.add(session_id)
        return list(seen)

    async def route_message(
        self,
        *,
        agent_id: str,
        message: dict[str, object],
        local_connections: list[tuple[WebSocket, str | None, str | None]],
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        local_sent = 0
        for ws, local_session_id, local_user_id in list(local_connections):
            if session_id is not None and local_session_id != session_id:
                continue
            if user_id is not None and local_user_id != user_id:
                continue
            try:
                await ws.send_json(message)
                local_sent += 1
            except Exception as exc:
                logger.warning(f"[Realtime] Failed to send local websocket message: {exc}")

        remote_targets: dict[str, int] = {}
        for record in await self._list_presence(agent_id):
            if record.get("instance_id") == self.instance_id:
                continue
            if session_id is not None and record.get("session_id") != session_id:
                continue
            if user_id is not None and record.get("user_id") != user_id:
                continue
            target_instance = record.get("instance_id")
            if target_instance:
                remote_targets[target_instance] = remote_targets.get(target_instance, 0) + 1

        if not remote_targets:
            return

        redis = await get_redis()
        envelope = json.dumps(
            {
                "message": message,
                "agent_id": agent_id,
                "session_id": session_id,
                "user_id": user_id,
                "origin_instance_id": self.instance_id,
            }
        )
        publish_tasks = [
            redis.publish(f"{PUBSUB_PREFIX}:instance:{instance_id}", envelope) for instance_id in remote_targets
        ]
        _ = await asyncio.gather(*publish_tasks, return_exceptions=True)
        logger.debug(
            f"[Realtime] Routed agent={agent_id} local={local_sent} remote_instances={list(remote_targets.keys())}"
        )

    async def start(self, deliver_local: LocalDeliverer) -> None:
        if self._started:
            return
        self._started = True
        self._subscriber_task = asyncio.create_task(self._subscriber_loop(deliver_local), name="realtime-subscriber")

    async def stop(self) -> None:
        if self._subscriber_task:
            task = self._subscriber_task
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    logger.warning(f"[Realtime] Subscriber task failed before shutdown: {exc}")
            else:
                _ = task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._subscriber_task = None
        self._started = False

    async def _subscriber_loop(self, deliver_local: LocalDeliverer) -> None:
        redis = await get_redis()
        pubsub = redis.pubsub()
        subscribed = False
        try:
            await pubsub.subscribe(self._instance_channel())
            subscribed = True
            while True:
                message_raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message_raw:
                    await asyncio.sleep(0.05)
                    continue
                try:
                    message = dict[str, object](message_raw)
                    payload_raw = message.get("data")
                    if isinstance(payload_raw, (bytes, bytearray)):
                        data = json_loads_object(bytes(payload_raw))
                    elif isinstance(payload_raw, str):
                        data = json_loads_object(payload_raw)
                    else:
                        data = json_object_from(payload_raw)
                    await deliver_local(
                        agent_id=data["agent_id"],
                        payload=data["message"],
                        session_id=data.get("session_id"),
                        user_id=data.get("user_id"),
                    )
                except Exception as exc:
                    logger.warning(f"[Realtime] Failed to deliver pubsub message: {exc}")
        except asyncio.CancelledError:
            raise
        finally:
            if subscribed:
                await pubsub.unsubscribe(self._instance_channel())
            await pubsub.aclose()

    async def _list_presence(self, agent_id: str) -> list[dict[str, str]]:
        redis_client: Redis = await get_redis()
        connection_ids = await redis_client.smembers(self._agent_index_key(agent_id))
        if not connection_ids:
            return []
        records: list[dict[str, str]] = []
        stale_ids: list[str] = []
        for connection_id in connection_ids:
            connection_id_text = _redis_text(connection_id)
            presence_raw: object = await redis_client.hgetall(self._connection_key(connection_id_text))
            if not presence_raw or not isinstance(presence_raw, dict):
                stale_ids.append(connection_id_text)
                continue
            records.append(
                {
                    _redis_text(key): _redis_text(value)
                    for key, value in presence_raw.items()
                    if isinstance(key, (str, bytes)) and isinstance(value, (str, bytes))
                }
            )
        if stale_ids:
            _ = await redis_client.srem(self._agent_index_key(agent_id), *stale_ids)
        return records


def _redis_text(value: str | bytes) -> str:
    match value:
        case str():
            return value
        case bytes():
            return value.decode()
        case unreachable:
            assert_never(unreachable)


realtime_router = RealtimeRouter()
