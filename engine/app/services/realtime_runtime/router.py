"""Redis-backed websocket presence and cross-instance message routing."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import assert_never, cast

from fastapi import WebSocket
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

from app.config import get_settings
from app.core.events import get_redis
from app.core.json_types import json_loads_object, json_object_from
from app.core.logging import logger
from app.schemas.chat_wire import ChatEventType
from app.services.realtime_runtime.chat_authorization import authorize_chat_delivery, read_shared_acl_version
from app.services.realtime_runtime.contracts import (
    LocalDeliverer,
    RealtimeEnvelope,
    RealtimePresence,
    ReceivedEnvelope,
    presence_adapter,
)
from app.services.realtime_runtime.fanout import encode_event_text, fan_out
from app.services.realtime_runtime.subscription import AcknowledgedPubSub

settings = get_settings()

PRESENCE_TTL_SECONDS = 180
PRESENCE_REFRESH_SECONDS = 60
PUBSUB_PREFIX = "realtime:ws"
# Per-socket ``send_text`` bounds. Chunks use the publisher's own tighter bound; every other event is bounded by default
# and the terminal events that end a turn wait longer so a slow but healthy client still receives them.
DEFAULT_SEND_TIMEOUT_SECONDS = 5.0
TERMINAL_SEND_TIMEOUT_SECONDS = 15.0
TERMINAL_EVENT_TYPES = frozenset({ChatEventType.DONE, ChatEventType.ERROR})

type ResubscribeHook = Callable[[], Awaitable[object]]


def resolve_send_timeout(message: Mapping[str, object]) -> float:
    """Return the ``ws.send_text`` bound for an event that carries no explicit one."""
    return (
        TERMINAL_SEND_TIMEOUT_SECONDS if message.get("type") in TERMINAL_EVENT_TYPES else DEFAULT_SEND_TIMEOUT_SECONDS
    )


class RealtimeRouter:
    def __init__(self) -> None:
        self.instance_id: str = settings.INSTANCE_ID
        self._subscriber_task: asyncio.Task[None] | None = None
        self._started: bool = False
        self._subscription: AcknowledgedPubSub | None = None
        self._acknowledgments: int = 0

    @property
    def ready(self) -> bool:
        return (
            self._started
            and self._subscriber_task is not None
            and not self._subscriber_task.done()
            and self._subscription is not None
            and self._subscription.acknowledged
            and self._subscription.connection is not None
            and bool(self._subscription.connection.is_connected)
        )

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
        payload: RealtimePresence = {
            "agent_id": agent_id,
            "session_id": session_id or "",
            "user_id": user_id or "",
            "instance_id": self.instance_id,
        }
        async with redis_client.pipeline(transaction=True) as pipe:
            self._queue_presence(pipe, agent_id, connection_id, payload)
            _ = await pipe.execute()
        websocket.state.realtime_connection_id = connection_id
        websocket.state.realtime_presence = payload
        return connection_id

    def _queue_presence(self, pipe: Pipeline, agent_id: str, connection_id: str, payload: RealtimePresence) -> None:
        connection_key = self._connection_key(connection_id)
        _ = pipe.sadd(self._agent_index_key(agent_id), connection_id)
        for field, value in payload.items():
            _ = pipe.hset(connection_key, field, value)
        _ = pipe.expire(connection_key, PRESENCE_TTL_SECONDS)
        _ = pipe.expire(self._agent_index_key(agent_id), PRESENCE_TTL_SECONDS)

    async def refresh_connection(self, *, agent_id: str, websocket: WebSocket) -> None:
        """Re-apply the presence TTL to a registered connection; no-op when it was never registered.

        If either key already expired (a stalled refresh), the presence is rewritten under the
        same connection id; ``SADD`` keeps the agent index free of duplicate members.
        """
        connection_id = getattr(websocket.state, "realtime_connection_id", None)
        if not isinstance(connection_id, str) or not connection_id:
            return
        redis_client: Redis = await get_redis()
        async with redis_client.pipeline(transaction=True) as pipe:
            _ = pipe.expire(self._connection_key(connection_id), PRESENCE_TTL_SECONDS)
            _ = pipe.expire(self._agent_index_key(agent_id), PRESENCE_TTL_SECONDS)
            refreshed = list[object](await pipe.execute())
        if all(refreshed):
            return
        payload = getattr(websocket.state, "realtime_presence", None)
        if not isinstance(payload, dict):
            return
        async with redis_client.pipeline(transaction=True) as pipe:
            self._queue_presence(pipe, agent_id, connection_id, cast("RealtimePresence", payload))
            _ = await pipe.execute()

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
        send_timeout: float | None = None,
    ) -> None:
        """Deliver locally while the presence lookup runs, then route to remote instances.

        Local sockets are served concurrently under ``LOCAL_FANOUT_CONCURRENCY``; the event is serialized once and,
        for a shared session, the ACL version is read once before any socket authorizes. ``send_timeout`` bounds
        only ``ws.send_text``, the one wait a client controls; without one, ``resolve_send_timeout`` picks the default
        or terminal bound. Authorization and Redis presence are never cancelled by it: aborting authorization discards
        the positive decision it would cache, and aborting routing skips remote instances. A stalled socket is skipped
        like any other failed send.
        """
        targets = [
            (ws, local_session_id)
            for ws, local_session_id, local_user_id in list(local_connections)
            if (session_id is None or local_session_id == session_id) and (user_id is None or local_user_id == user_id)
        ]
        presence = asyncio.create_task(self._remote_targets(agent_id, session_id, user_id), name="realtime-presence")
        try:
            bound = resolve_send_timeout(message) if send_timeout is None else send_timeout
            local_sent = await self._deliver_local(agent_id, message, targets, bound)
            remote_targets = await presence
        finally:
            if not presence.done():
                _ = presence.cancel()
        if not remote_targets:
            return

        redis = await get_redis()
        payload: RealtimeEnvelope = {
            "message": message,
            "agent_id": agent_id,
            "session_id": session_id,
            "user_id": user_id,
            "origin_instance_id": self.instance_id,
        }
        envelope = json.dumps(payload)
        publish_tasks = [
            redis.publish(f"{PUBSUB_PREFIX}:instance:{instance_id}", envelope) for instance_id in remote_targets
        ]
        _ = await asyncio.gather(*publish_tasks, return_exceptions=True)
        logger.debug(
            f"[Realtime] Routed agent={agent_id} local={local_sent} remote_instances={list(remote_targets.keys())}"
        )

    @staticmethod
    async def _deliver_local(
        agent_id: str,
        message: dict[str, object],
        targets: list[tuple[WebSocket, str | None]],
        send_timeout: float,
    ) -> int:
        if not targets:
            return 0
        text = encode_event_text(message)
        acl_version = await read_shared_acl_version(agent_id, (target_session for _, target_session in targets))

        async def deliver(target: tuple[WebSocket, str | None]) -> bool:
            ws, target_session = target
            try:
                if not await authorize_chat_delivery(ws, agent_id, target_session, acl_version=acl_version):
                    return False
                async with asyncio.timeout(send_timeout):
                    await ws.send_text(text)
            except Exception as exc:
                logger.warning(f"[Realtime] Failed to send local websocket message: {exc!r}")
                return False
            return True

        return await fan_out(targets, deliver)

    async def _remote_targets(self, agent_id: str, session_id: str | None, user_id: str | None) -> dict[str, int]:
        remote_targets: dict[str, int] = {}
        for record in await self._list_presence(agent_id):
            if record.get("instance_id") == self.instance_id:
                continue
            if session_id is not None and record.get("session_id") != session_id:
                continue
            if user_id is not None and record.get("user_id") != user_id:
                continue
            target_instance = record.get("instance_id")
            if target_instance not in (None, ""):
                remote_targets[target_instance] = remote_targets.get(target_instance, 0) + 1
        return remote_targets

    async def start(self, deliver_local: LocalDeliverer, on_resubscribe: ResubscribeHook | None = None) -> None:
        """Start the subscriber; ``on_resubscribe`` runs after every subscribe acknowledgment except the first.

        Events published while the subscription was down are gone, so the hook is where local clients are told to
        reconnect and refetch history. It runs inside the serial subscriber loop, ahead of the next event.
        """
        if self._started:
            return
        self._started = True
        self._acknowledgments = 0
        self._subscriber_task = asyncio.create_task(
            self._subscriber_loop(deliver_local, on_resubscribe), name="realtime-subscriber"
        )

    async def stop(self) -> None:
        self._started = False
        if self._subscription is not None:
            self._subscription.acknowledged = False
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
        self._subscription = None
        self._started = False

    async def _subscriber_loop(self, deliver_local: LocalDeliverer, on_resubscribe: ResubscribeHook | None) -> None:
        redis = await get_redis()
        while True:
            pubsub = AcknowledgedPubSub(redis.connection_pool)
            self._subscription = pubsub
            try:
                await pubsub.subscribe(self._instance_channel())
                while True:
                    message_raw = await pubsub.get_message(ignore_subscribe_messages=False, timeout=1.0)
                    if not message_raw:
                        continue
                    message = dict[str, object](message_raw)
                    channel = message.get("channel")
                    if message.get("type") == "subscribe":
                        pubsub.acknowledged = channel in (self._instance_channel(), self._instance_channel().encode())
                        if pubsub.acknowledged:
                            await self._acknowledged(on_resubscribe)
                    elif message.get("type") == "unsubscribe":
                        pubsub.acknowledged = False
                    elif message.get("type") == "message":
                        await self._deliver_message(message.get("data"), deliver_local)
            except Exception as exc:
                logger.warning("[Realtime] Subscription lost: {}", type(exc).__name__)
            finally:
                pubsub.acknowledged = False
                await pubsub.aclose()
            await asyncio.sleep(1.0)

    async def _acknowledged(self, on_resubscribe: ResubscribeHook | None) -> None:
        self._acknowledgments += 1
        if self._acknowledgments == 1 or on_resubscribe is None:
            return
        try:
            await on_resubscribe()
        except Exception as exc:
            logger.warning("[Realtime] Resubscribe hook failed: {}", type(exc).__name__)

    @staticmethod
    async def _deliver_message(payload_raw: object, deliver_local: Callable[..., Awaitable[object]]) -> None:
        """Keep the legacy receive boundary permissive; registration uses LocalDeliverer."""
        try:
            if isinstance(payload_raw, (bytes, bytearray)):
                data = json_loads_object(bytes(payload_raw))
            elif isinstance(payload_raw, str):
                data = json_loads_object(payload_raw)
            else:
                data = json_object_from(payload_raw)
            envelope = ReceivedEnvelope(
                agent_id=data["agent_id"],
                message=data["message"],
                session_id=data.get("session_id"),
                user_id=data.get("user_id"),
            )
            await deliver_local(
                agent_id=envelope.agent_id,
                payload=envelope.message,
                session_id=envelope.session_id,
                user_id=envelope.user_id,
            )
        except Exception as exc:
            logger.warning("[Realtime] Failed to deliver pubsub message: {}", exc)

    async def _list_presence(self, agent_id: str) -> list[RealtimePresence]:
        redis_client: Redis = await get_redis()
        connection_ids = await redis_client.smembers(self._agent_index_key(agent_id))
        if not connection_ids:
            return []
        connection_id_texts = [_redis_text(connection_id) for connection_id in connection_ids]
        async with redis_client.pipeline(transaction=False) as pipe:
            for connection_id_text in connection_id_texts:
                _ = pipe.hgetall(self._connection_key(connection_id_text))
            presence_rows = cast("list[dict[str | bytes, str | bytes]]", await pipe.execute())
        records: list[RealtimePresence] = []
        stale_ids: list[str] = []
        for connection_id_text, presence_raw in zip(connection_id_texts, presence_rows, strict=True):
            if not presence_raw:
                stale_ids.append(connection_id_text)
                continue
            records.append(
                presence_adapter.validate_python(
                    {_redis_text(key): _redis_text(value) for key, value in presence_raw.items()}, strict=True
                )
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
