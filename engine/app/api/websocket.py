"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import contextlib
import time
import uuid
import weakref
from collections.abc import Callable
from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.core.incident_reporting import report_exception
from app.core.json_types import is_str_dict, json_as_str, json_loads_value
from app.core.logging import logger, set_trace_id
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
from app.schemas.chat_wire import ChatEventType, serialize_chat_server_event
from app.services import chat_tickets
from app.services.chat_persist import persist_chat_message
from app.services.chat_submissions import (
    ChatSubmission,
    InvalidChatAttachmentsError,
    SubmissionConflictError,
    accept_submission,
)
from app.services.llm.types import OpenAIMessage
from app.services.llm.utils import convert_chat_messages_to_llm_format
from app.services.onboarding import is_onboarded, try_begin_onboarding_greeting
from app.services.onboarding_greeting import build_greeting_request
from app.services.openclaw_gateway_delivery import CHUNK_SEND_TIMEOUT_SECONDS
from app.services.openclaw_routing import (
    GuestConfigWriteError,
    NoCompanyModelError,
    enqueue_prepared_openclaw_message,
    prepare_openclaw_message,
)
from app.services.openclaw_update_gate import OpenClawUpdateGateError
from app.services.quota_guard import (
    AgentExpired,
    QuotaExceeded,
    check_agent_expired,
)
from app.services.realtime import realtime_router
from app.services.realtime_runtime.chat_authorization import (
    DeliveryVerdict,
    authorize_chat_delivery,
    read_delivery_acl_version,
)
from app.services.realtime_runtime.contracts import RoutingOutcome
from app.services.realtime_runtime.fanout import encode_event_text, fan_out
from app.services.realtime_runtime.router import (
    DEFAULT_SEND_TIMEOUT_SECONDS,
    PRESENCE_REFRESH_SECONDS,
    resolve_authorization_wait,
    resolve_send_timeout,
)
from app.services.shared_chat import (
    SessionNotWritableError,
    is_canonical,
    read_only_refusal,
    require_writable_session,
)

router = APIRouter(tags=["websocket"])


type WebSocketConnection = tuple[WebSocket, str | None, str | None]
type RealtimeMessage = dict[str, object]

_presence_sleep = asyncio.sleep
_heartbeat_sleep = asyncio.sleep
# The web-e client reconnects after 25 s without a frame; keep this well under that deadline.
HEARTBEAT_INTERVAL_SECONDS = 10.0
# Not 4001/4003: the client treats every other close as transient, reconnects with a fresh ticket and refetches history.
RESUBSCRIBE_CLOSE_CODE = 1012
# Consecutive send timeouts, with no successful send in between, that evict a socket: one slow chunk on a healthy
# phone survives, a client that stopped reading stops costing every later event its send bound.
STALLED_SEND_EVICTION_THRESHOLD = 3
# Reconnectable like RESUBSCRIBE_CLOSE_CODE but distinct from it ("try again later"); never 4001/4003.
EVICTION_CLOSE_CODE = 1013
# Bounds each step of an eviction (presence removal, then the close): closing a peer that is not reading can block
# just like the send did.
EVICTION_CLOSE_TIMEOUT_SECONDS = DEFAULT_SEND_TIMEOUT_SECONDS


def _json_object_payload(value: object) -> dict[str, object]:
    if not is_str_dict(value):
        raise TypeError("JSON object required")
    return {name: item for key, item in value.items() if (name := json_as_str(key)) is not None}


def _payload_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


async def _send_chat_event(websocket: WebSocket, message: RealtimeMessage) -> None:
    await websocket.send_json(serialize_chat_server_event(message))


class ConnectionManager:
    """Manage WebSocket connections per agent, evict the ones whose sends keep timing out and close the revoked ones.

    The manager is the ``SendObserver`` of both delivery paths: the router reports each bounded send and each revoked
    socket back to it and holds no socket state itself.
    """

    def __init__(self):
        # agent_id_str -> list of (WebSocket, session_id_str | None, user_id_str | None)
        self.active_connections: dict[str, list[WebSocketConnection]] = {}
        # Consecutive send timeouts of listed sockets; a successful send, eviction or disconnect drops the entry.
        self._send_timeouts: dict[WebSocket, int] = {}
        # Sockets withdrawn from delivery: evicted after stalling, or closed because their canonical-session access ended.
        self._withdrawn: weakref.WeakSet[WebSocket] = weakref.WeakSet()
        # Presence ids of withdrawn sockets until their Redis removal finished; this instance's presence reads skip them.
        self._withdrawn_presence: set[str] = set()
        self._eviction_tasks: set[asyncio.Task[None]] = set()

    async def connect(
        self, agent_id: str, websocket: WebSocket, session_id: str | None = None, user_id: str | None = None
    ):
        _ = await realtime_router.register_connection(
            agent_id=agent_id,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
        )
        if agent_id not in self.active_connections:
            self.active_connections[agent_id] = []
        self.active_connections[agent_id].append((websocket, session_id, user_id))

    async def disconnect(self, agent_id: str, websocket: WebSocket):
        """Release a socket. Idempotent with eviction in either order: an evicted socket carries no presence id, and a
        socket that is no longer listed cannot be evicted."""
        self._remove_local(agent_id, websocket)
        _ = self._send_timeouts.pop(websocket, None)
        try:
            await realtime_router.unregister_connection(agent_id=agent_id, websocket=websocket)
        except RedisError as error:
            report_exception(error, source="websocket", operation="disconnect")
            logger.warning("[WS] Failed to unregister connection for agent {}: {}", agent_id, error)

    def _remove_local(self, agent_id: str, websocket: WebSocket) -> None:
        if agent_id in self.active_connections:
            self.active_connections[agent_id] = [
                (ws, sid, uid) for ws, sid, uid in self.active_connections[agent_id] if ws != websocket
            ]

    def is_live(self, websocket: WebSocket) -> bool:
        return websocket not in self._withdrawn

    def sent(self, websocket: WebSocket) -> None:
        if self._send_timeouts:  # empty while every socket keeps up, so a healthy send costs no lookup
            _ = self._send_timeouts.pop(websocket, None)

    def timed_out(self, agent_id: str, websocket: WebSocket) -> bool:
        """Count a send that hit its bound and evict on the ``STALLED_SEND_EVICTION_THRESHOLD``-th in a row.

        A socket already withdrawn, or no longer listed because its handler disconnected, is not counted.
        """
        if not self._listed(agent_id, websocket):
            return False
        timeouts = self._send_timeouts.get(websocket, 0) + 1
        if timeouts < STALLED_SEND_EVICTION_THRESHOLD:
            self._send_timeouts[websocket] = timeouts
            return False
        self._withdraw(agent_id, websocket, EVICTION_CLOSE_CODE, name="ws-eviction")
        logger.warning(
            "[WS] Evicted chat socket for agent {} after {} consecutive send timeouts",
            agent_id,
            STALLED_SEND_EVICTION_THRESHOLD,
        )
        return True

    def refused(self, agent_id: str, websocket: WebSocket, close_code: int) -> None:
        """Withdraw a socket whose canonical-session access was revoked and close it with ``close_code`` (4001 or 4003).

        Idempotent with eviction and ``disconnect`` in any order: a socket already withdrawn, or no longer listed
        because its handler disconnected, is left alone, so it is closed at most once.
        """
        if not self._listed(agent_id, websocket):
            return
        self._withdraw(agent_id, websocket, close_code, name="ws-revocation")
        logger.info(
            "[WS] Closing chat socket for agent {} with {}: canonical session access revoked", agent_id, close_code
        )

    def _listed(self, agent_id: str, websocket: WebSocket) -> bool:
        return websocket not in self._withdrawn and any(
            ws is websocket for ws, _, _ in self._local_connections(agent_id)
        )

    def _withdraw(self, agent_id: str, websocket: WebSocket, close_code: int, *, name: str) -> None:
        """Withdraw the socket from delivery and presence without awaiting, then close it in an owned, bounded task."""
        self._withdrawn.add(websocket)
        _ = self._send_timeouts.pop(websocket, None)
        self._remove_local(agent_id, websocket)
        connection_id_raw = getattr(websocket.state, "realtime_connection_id", None)
        connection_id = connection_id_raw if isinstance(connection_id_raw, str) and connection_id_raw else None
        # The handler's presence refresh rewrites expired presence under this id; cleared, it has nothing to rewrite.
        websocket.state.realtime_connection_id = None
        websocket.state.realtime_presence = None
        if connection_id is not None:
            self._withdrawn_presence.add(connection_id)
        task = asyncio.create_task(self._close_withdrawn(agent_id, websocket, connection_id, close_code), name=name)
        self._eviction_tasks.add(task)
        task.add_done_callback(self._eviction_tasks.discard)

    async def _close_withdrawn(
        self, agent_id: str, websocket: WebSocket, connection_id: str | None, close_code: int
    ) -> None:
        """Remove the withdrawn socket's Redis presence, then close it with ``close_code``; each step is bounded."""
        if connection_id is not None:
            try:
                async with asyncio.timeout(EVICTION_CLOSE_TIMEOUT_SECONDS):
                    await realtime_router.unregister_connection_id(agent_id=agent_id, connection_id=connection_id)
            except Exception as error:
                report_exception(error, source="websocket", operation="eviction-presence")
                logger.warning(
                    "[WS] Failed to remove presence of evicted socket for agent {}: {}", agent_id, type(error).__name__
                )
            finally:
                # A record left behind expires with its TTL: the refresh can no longer find its id.
                self._withdrawn_presence.discard(connection_id)
        if WebSocketState.DISCONNECTED in (websocket.application_state, websocket.client_state):
            return
        try:
            async with asyncio.timeout(EVICTION_CLOSE_TIMEOUT_SECONDS):
                await websocket.close(code=close_code)
        except Exception as error:
            logger.warning("[WS] Failed to close withdrawn socket for agent {}: {}", agent_id, type(error).__name__)

    async def drain_evictions(self) -> None:
        """Wait for the eviction and revocation closes in flight; every step of one is bounded, so this returns within
        those bounds."""
        while self._eviction_tasks:
            _ = await asyncio.gather(*self._eviction_tasks, return_exceptions=True)

    async def refresh_presence(self, agent_id: str, websocket: WebSocket) -> None:
        try:
            await realtime_router.refresh_connection(agent_id=agent_id, websocket=websocket)
        except RedisError as error:
            report_exception(error, source="websocket", operation="presence-refresh")
            logger.warning("[WS] Failed to refresh presence for agent {}: {}", agent_id, error)

    def _local_connections(self, agent_id: str) -> list[WebSocketConnection]:
        return self.active_connections.get(agent_id, [])

    async def deliver_pubsub_message(
        self,
        *,
        agent_id: str,
        payload: RealtimeMessage,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Deliver one received event to the matching local sockets before the subscriber takes the next one.

        Sockets are served concurrently under ``LOCAL_FANOUT_CONCURRENCY``. Every send is bounded: chunks get the
        same bound the publisher uses and other events the default or terminal bound, so a stalled receiver delays
        the subscriber loop by at most that bound, and only until ``STALLED_SEND_EVICTION_THRESHOLD`` timeouts in a
        row evict it. A canonical-session socket waits for its authorization decision only until
        ``resolve_authorization_wait`` after that decision started: a late or transiently failed decision skips the
        socket for this event only, and a revoked one is withdrawn and closed by ``refused``.
        """
        targets = [
            (ws, sid)
            for ws, sid, uid in list(self.active_connections.get(agent_id, []))
            if (session_id is None or sid == session_id) and (user_id is None or uid == user_id)
        ]
        if not targets:
            return
        try:
            text = encode_event_text(serialize_chat_server_event(payload))
        except Exception as error:
            report_exception(error, source="websocket", operation="pubsub-delivery")
            logger.warning(f"[WS] Failed to encode message for connected clients: {error}")
            return
        acl_version = await read_delivery_acl_version(agent_id, targets)
        send_timeout = (
            CHUNK_SEND_TIMEOUT_SECONDS if payload.get("type") == ChatEventType.CHUNK else resolve_send_timeout(payload)
        )
        authorization_wait = resolve_authorization_wait(payload)

        async def deliver(target: tuple[WebSocket, str | None]) -> bool:
            ws, sid = target
            try:
                verdict = await authorize_chat_delivery(
                    ws, agent_id, sid, acl_version=acl_version, wait_seconds=authorization_wait
                )
            except Exception as error:
                report_exception(error, source="websocket", operation="pubsub-delivery")
                logger.warning(f"[WS] Failed to authorize message for connected client: {error}")
                return False
            if verdict is not DeliveryVerdict.ALLOWED:
                if verdict.close_code is not None:
                    self.refused(agent_id, ws, verdict.close_code)
                return False
            # Authorization may have awaited while the publisher path evicted this socket.
            if not self.is_live(ws):
                return False
            try:
                async with asyncio.timeout(send_timeout):
                    await ws.send_text(text)
            except TimeoutError as error:
                report_exception(error, source="websocket", operation="pubsub-delivery")
                evicted = self.timed_out(agent_id, ws)
                logger.warning(
                    "[WS] Send to connected client timed out after {}s agent={} evicted={}",
                    send_timeout,
                    agent_id,
                    evicted,
                )
                return False
            except Exception as error:
                report_exception(error, source="websocket", operation="pubsub-delivery")
                logger.warning(f"[WS] Failed to send message to connected client: {error}")
                return False
            self.sent(ws)
            return True

        _ = await fan_out(targets, deliver)

    async def close_local_connections(self) -> None:
        """Close every local chat socket with a reconnectable code so each client refetches what a lost subscription dropped."""
        sockets = [ws for connections in list(self.active_connections.values()) for ws, _, _ in list(connections)]

        async def close(ws: WebSocket) -> bool:
            try:
                async with asyncio.timeout(DEFAULT_SEND_TIMEOUT_SECONDS):
                    await ws.close(code=RESUBSCRIBE_CLOSE_CODE)
            except Exception as error:
                report_exception(error, source="websocket", operation="resubscribe-close")
                logger.warning(f"[WS] Failed to close connected client after resubscribe: {error}")
                return False
            return True

        _ = await fan_out(sockets, close)

    async def send_message(self, agent_id: str, message: RealtimeMessage) -> RoutingOutcome:
        return await realtime_router.route_message(
            agent_id=agent_id,
            message={**serialize_chat_server_event(message)},
            local_connections=self._local_connections(agent_id),
            observer=self,
        )

    async def send_to_session(
        self,
        agent_id: str,
        session_id: str,
        message: RealtimeMessage,
        user_id: str | None = None,
        send_timeout: float | None = None,
    ) -> RoutingOutcome:
        """Send only to this session, optionally restricting it to one user's sockets.

        ``send_timeout`` bounds each local socket send, nothing else (see ``RealtimeRouter.route_message``).
        """
        wanted = session_id.strip().lower()
        return await realtime_router.route_message(
            agent_id=agent_id,
            message={**serialize_chat_server_event(message)},
            local_connections=self._local_connections(agent_id),
            session_id=wanted,
            user_id=user_id,
            send_timeout=send_timeout,
            observer=self,
        )

    async def send_to_user(self, agent_id: str, user_id: str, message: RealtimeMessage) -> RoutingOutcome:
        """Send message to all live WebSocket sessions of a given platform user for an agent."""
        return await realtime_router.route_message(
            agent_id=agent_id,
            message={**serialize_chat_server_event(message)},
            local_connections=self._local_connections(agent_id),
            user_id=user_id,
            observer=self,
        )

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        """Return distinct session IDs for all active WS connections of an agent."""
        return await realtime_router.get_active_session_ids(agent_id, hidden=self._withdrawn_presence)

    async def is_user_viewing_session(self, agent_id: str, session_id: str, user_id: str) -> bool:
        """Return True if the given platform user currently has this exact session open."""
        return await realtime_router.is_user_viewing_session(
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            hidden=self._withdrawn_presence,
        )


manager = ConnectionManager()


async def maybe_mark_session_read_for_active_viewer(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID,
    guard: Callable[[], None] | None = None,
) -> bool:
    """Advance last_read_at_by_user if the owner is actively viewing this exact session."""
    if not await manager.is_user_viewing_session(str(agent_id), session_id, str(user_id)):
        return False

    try:
        sid = uuid.UUID(session_id)
    except ValueError, TypeError:
        return False
    session = await chat_session_dao.get(sid)
    if not session:
        return False

    if guard is not None:
        guard()
    _ = await chat_session_dao.update(db_obj=session, obj_in={"last_read_at_by_user": datetime.now(UTC)})
    return True


@router.websocket("/ws/chat/{agent_id}")
async def websocket_chat(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    ticket: str = Query(""),
    session_id: str = Query(None),
    lang: str = Query("en"),
    heartbeat: str = Query(""),
):
    """WebSocket endpoint for real-time chat with an agent; ``heartbeat=1`` opts in to keepalive frames."""
    handler = WebSocketChatHandler(websocket, agent_id, ticket, session_id, lang, heartbeat == "1")
    await handler.run()


class WebSocketChatHandler:
    """Manages connection lifecycle, inbound persistence, and OpenClaw enqueue for a user-agent session."""

    user: UserRecord
    agent: AgentRecord
    conv_id: str
    access_level: str

    def __init__(
        self,
        websocket: WebSocket,
        agent_id: uuid.UUID,
        ticket: str,
        session_id: str | None = None,
        lang: str = "en",
        heartbeat: bool = False,
    ):
        self.websocket: WebSocket = websocket
        self.agent_id: uuid.UUID = agent_id
        self.ticket: str = ticket
        self.binding: chat_tickets.ChatTicketBinding
        self.session_id_param: str | None = session_id
        self.lang: str = lang
        self.heartbeat: bool = heartbeat
        self._presence_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

        # State fields initialized during setup
        self.agent_name: str = ""
        self.role_description: str = ""
        self.welcome_message: str = ""
        self.ctx_size: int = 100
        self.user_display_name: str = ""
        self.history_messages: list[ChatMessageRecord] = []
        self._history_unavailable: bool = False
        self.conversation: list[OpenAIMessage] = []
        self.current_user_text: str = ""

    async def run(self):
        """Main entry point for handling the lifecycle of the WebSocket connection."""
        try:
            try:
                await self.websocket.accept()
                if not self.ticket:
                    raise HTTPException(400, "Chat ticket required")
                self.binding = await chat_tickets.redeem(self.ticket)
                self.ticket = ""
                if (
                    str(self.binding.agent_id) != str(self.agent_id)
                    or str(self.binding.session_id) != self.session_id_param
                ):
                    raise HTTPException(400, "Chat ticket binding mismatch")
                remaining = self.binding.auth_deadline - time.time()
                if remaining <= 0:
                    raise HTTPException(401, "Authentication expired")
                with anyio.fail_after(remaining):
                    if not await self.setup():
                        return
                    await self.message_loop()
            except TimeoutError:
                await self._close(4001)
            except HTTPException as exc:
                code = {401: 4001, 403: 4003, 404: 4003, 503: 1011}.get(exc.status_code, 4002)
                await self._send_error({"type": ChatEventType.ERROR, "content": exc.detail})
                await self._close(code)
            except WebSocketDisconnect:
                pass
            except Exception as error:
                if manager.is_live(self.websocket):
                    report_exception(error, source="websocket", operation="chat-lifecycle")
                    logger.exception("[WS] Unexpected chat error")
                else:
                    logger.info("[WS] Withdrawn chat socket ended: {}", type(error).__name__)
                await self._close(1011)
        except WebSocketDisconnect:
            pass
        finally:
            with anyio.CancelScope(shield=True):
                await self._stop_heartbeat()
                await self._stop_presence_refresh()
                await manager.disconnect(str(self.agent_id), self.websocket)

    def _ended(self) -> bool:
        """Eviction, a revocation or either side already closed the socket, so another send or close could only raise."""
        return not manager.is_live(self.websocket) or WebSocketState.DISCONNECTED in (
            self.websocket.application_state,
            self.websocket.client_state,
        )

    async def _close(self, code: int) -> None:
        if self._ended():
            return
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self.websocket.close(code=code)

    async def _send_error(self, message: RealtimeMessage) -> None:
        if self._ended():
            return
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await _send_chat_event(self.websocket, message)

    async def _refresh_presence(self) -> None:
        """Keep Redis presence alive; the TTL would otherwise end cross-instance delivery."""
        while True:
            await _presence_sleep(PRESENCE_REFRESH_SECONDS)
            try:
                await manager.refresh_presence(str(self.agent_id), self.websocket)
            except Exception as error:
                logger.warning("[WS] Presence refresh failed: {}", type(error).__name__)

    async def _stop_presence_refresh(self) -> None:
        task, self._presence_task = self._presence_task, None
        if task is None:
            return
        _ = task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            report_exception(error, source="websocket", operation="presence-refresh")
            logger.warning("[WS] Presence refresh task failed for agent {}: {}", self.agent_id, error)

    async def _send_heartbeats(self) -> None:
        """Keep an opted-in client's liveness deadline fed without touching the database or Redis."""
        try:
            while True:
                await _heartbeat_sleep(HEARTBEAT_INTERVAL_SECONDS)
                async with asyncio.timeout(DEFAULT_SEND_TIMEOUT_SECONDS):
                    await _send_chat_event(self.websocket, {"type": ChatEventType.HEARTBEAT})
        except Exception as error:
            logger.warning("[WS] Heartbeat stopped for agent {}: {}", self.agent_id, type(error).__name__)

    async def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None:
            return
        _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def setup(self) -> bool:
        self.user, self.agent, self.access_level = await chat_tickets.authorize(self.binding)
        _ = await chat_tickets.authorized_session(self.binding, self.user, self.agent)
        self.websocket.state.chat_binding = self.binding
        self.conv_id = str(self.binding.session_id)
        self.agent_name = self.agent.name
        self.role_description = self.agent.role_description or ""
        self.welcome_message = self.agent.welcome_message or ""
        self.ctx_size = self.agent.context_window_size or 100
        self.user_display_name = (self.user.display_name or "").strip() or "there"
        await self._load_history()
        await manager.connect(str(self.agent_id), self.websocket, self.conv_id, str(self.user.id))
        self._presence_task = asyncio.create_task(self._refresh_presence(), name="ws-presence-refresh")
        await _send_chat_event(self.websocket, {"type": ChatEventType.CONNECTED, "session_id": self.conv_id})
        if self.heartbeat:
            self._heartbeat_task = asyncio.create_task(self._send_heartbeats(), name="ws-heartbeat")
        self.conversation = self._build_conversation_context()
        return True

    async def _load_history(self):
        """Loads and prepares history messages for the conversation."""
        try:
            self.history_messages = list(
                await chat_message_dao.list_recent(
                    agent_id=self.agent_id,
                    conversation_id=self.conv_id,
                    limit=self.ctx_size,
                )
            )
            logger.info(f"[WS] Loaded {len(self.history_messages)} history messages for session {self.conv_id}")
        except Exception as e:
            self._history_unavailable = True
            report_exception(e, source="websocket", operation="load-history")
            logger.warning(f"[WS] History load failed (non-fatal): {e}")

    def _build_conversation_context(self) -> list[OpenAIMessage]:
        """Translates historical ChatMessages to LLM inputs."""
        return convert_chat_messages_to_llm_format(self.history_messages)

    async def message_loop(self):
        """Core message processing loop."""
        # Send welcome message on new session (no history)
        if self.welcome_message and not self.history_messages and not self._history_unavailable:
            await _send_chat_event(
                self.websocket, {"type": ChatEventType.DONE, "role": "assistant", "content": self.welcome_message}
            )

        while True:
            try:
                data = _json_object_payload(json_loads_value(await self.websocket.receive_text()))
            except KeyError, TypeError, ValueError:
                await _send_chat_event(
                    self.websocket,
                    {"type": ChatEventType.ERROR, "content": "Invalid chat message. Expected a JSON object."},
                )
                continue

            # Set a unique trace ID for this specific message processing.
            trace_id = str(uuid.uuid4())[:12]
            set_trace_id(trace_id)

            content = _payload_str(data.get("content", ""))
            display_content = _payload_str(data.get("display_content", ""))
            file_name = _payload_str(data.get("file_name", ""))
            is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
            logger.info(f"[WS] Received: {content[:50]}" + (" [onboarding]" if is_onboarding_trigger else ""))

            try:
                self.user, self.agent, self.access_level = await chat_tickets.authorize(self.binding)
                _ = await chat_tickets.authorized_session(self.binding, self.user, self.agent)
            except HTTPException as exc:
                await self._send_error({"type": ChatEventType.ERROR, "content": exc.detail})
                await self._close(4001 if exc.status_code == 401 else 4003)
                return

            if not is_onboarding_trigger:
                try:
                    submission = ChatSubmission.model_validate(
                        {
                            "client_submission_id": _payload_str(data.get("client_submission_id")),
                            "content": content,
                            "display_content": display_content,
                            "file_name": file_name,
                            "attachments": data.get("attachments", []),
                        }
                    )
                except ValidationError as exc:
                    rejection: RealtimeMessage
                    if any(error["loc"][0] == "client_submission_id" for error in exc.errors()):
                        rejection = {
                            "type": ChatEventType.TURN_REJECTED,
                            "reason": "invalid_submission_id",
                            "content": "Submission UUID required.",
                        }
                    else:
                        rejection = {
                            "type": ChatEventType.TURN_REJECTED,
                            "reason": "invalid_attachments",
                            "client_submission_id": _payload_str(data.get("client_submission_id")),
                            "content": "Attachment references must be valid agent-relative files.",
                        }
                    await _send_chat_event(self.websocket, rejection)
                    continue
                if not await self._handle_submission(submission):
                    return
                continue

            if await self._handle_onboarding_trigger_guard():
                continue
            session = await chat_session_dao.get_for_agent(self.binding.session_id, self.agent_id)
            if session is None:
                raise HTTPException(403, "Session unavailable")
            try:
                require_writable_session(session, self.user.id)
            except SessionNotWritableError as refused:
                raise await read_only_refusal(refused) from None
            if is_canonical(session):
                await self._load_history()
                self.conversation = self._build_conversation_context()
            content = "Please begin the onboarding."

            self.current_user_text = content

            if not await self._check_quotas():
                continue

            try:
                prepared = await prepare_openclaw_message(
                    agent=self.agent,
                    content=await build_greeting_request(self.agent, self.user),
                    history=self.conversation,
                )
            except NoCompanyModelError as exc:
                logger.warning("[WS] OpenClaw preparation blocked: {}", exc)
                await _send_chat_event(
                    self.websocket,
                    {"type": ChatEventType.TURN_REJECTED, "reason": "no_company_model", "content": str(exc)},
                )
                continue
            except OpenClawUpdateGateError as gate:
                await _send_chat_event(self.websocket, {"type": ChatEventType.TURN_REJECTED, **gate.rejection()})
                continue
            except GuestConfigWriteError as error:
                report_exception(error, source="websocket", operation="onboarding-prepare")
                logger.warning("[WS] Guest config preparation failed for {}", self.agent_id)
                await _send_chat_event(
                    self.websocket,
                    {
                        "type": ChatEventType.TURN_REJECTED,
                        "reason": "guest_config_failed",
                        "content": "Guest configuration could not be updated. Please try again.",
                    },
                )
                continue

            try:
                async with connection_ctx():
                    session = await chat_session_dao.lock_for_agent(self.binding.session_id, self.agent_id)
                    if session is None:
                        raise HTTPException(403, "Session unavailable")
                    require_writable_session(session, self.user.id)
                    if not await try_begin_onboarding_greeting(self.agent_id, self.user.id):
                        await _send_chat_event(
                            self.websocket, {"type": ChatEventType.ONBOARDED, "agent_id": str(self.agent_id)}
                        )
                        continue
                    await self._save_user_message(content, display_content, file_name, is_onboarding_trigger)
                    _ = await enqueue_prepared_openclaw_message(
                        prepared, sender_user_id=self.user.id, conversation_id=self.conv_id
                    )
            except QuotaExceeded as exc:
                await _send_chat_event(
                    self.websocket,
                    {"type": ChatEventType.TURN_REJECTED, "reason": "quota_exceeded", "content": exc.message},
                )
                continue
            except SessionNotWritableError as refused:
                raise await read_only_refusal(refused) from None
            except Exception as error:
                report_exception(error, source="websocket", operation="onboarding-turn")
                logger.exception("[WS] Turn transaction failed")
                await _send_chat_event(
                    self.websocket,
                    {
                        "type": ChatEventType.TURN_REJECTED,
                        "reason": "enqueue_failed",
                        "content": "Your message could not be sent. Please try again.",
                    },
                )
                continue

            self.conversation.append({"role": "user", "content": content})
            await _send_chat_event(self.websocket, {"type": ChatEventType.STATUS, "content": "thinking"})
            logger.info("[WS] OpenClaw: message queued for gateway poll")

    async def _handle_submission(self, submission: ChatSubmission) -> bool:
        rejection: RealtimeMessage = {
            "type": ChatEventType.TURN_REJECTED,
            "client_submission_id": str(submission.client_submission_id),
        }
        try:
            accepted = await accept_submission(
                submission,
                user=self.user,
                agent=self.agent,
                session_id=self.binding.session_id,
                history=self.conversation,
            )
        except SubmissionConflictError:
            rejection.update(reason="submission_conflict", content="Submission UUID already used for another message.")
        except InvalidChatAttachmentsError:
            rejection.update(reason="invalid_attachments", content="An attachment file is unavailable.")
        except NoCompanyModelError as exc:
            rejection.update(reason="no_company_model", content=str(exc))
        except OpenClawUpdateGateError as gate:
            rejection.update(gate.rejection())
        except GuestConfigWriteError as error:
            report_exception(error, source="websocket", operation="submission-prepare")
            logger.warning("[WS] Guest config preparation failed for {}", self.agent_id)
            rejection.update(
                reason="guest_config_failed", content="Guest configuration could not be updated. Please try again."
            )
        except AgentExpired as exc:
            rejection.update(reason="agent_expired", content=exc.message)
        except QuotaExceeded as exc:
            rejection.update(reason="quota_exceeded", content=exc.message)
        except HTTPException as exc:
            await self._send_error({"type": ChatEventType.ERROR, "content": exc.detail})
            await self._close(4001 if exc.status_code == 401 else 4003)
            return False
        except Exception as error:
            report_exception(error, source="websocket", operation="submission-turn")
            logger.exception("[WS] Turn transaction failed")
            rejection.update(reason="enqueue_failed", content="Your message could not be sent. Please try again.")
        else:
            await _send_chat_event(
                self.websocket,
                {
                    "type": ChatEventType.TURN_ACCEPTED,
                    "message_id": str(accepted.message.id),
                    "session_id": str(self.binding.session_id),
                    "client_submission_id": str(submission.client_submission_id),
                },
            )
            if not accepted.replayed:
                self.current_user_text = submission.content
                self.conversation.append({"role": "user", "content": submission.content})
                if accepted.canonical:
                    # Echo the turn to the owner's other tabs; only the owner's sockets may receive it.
                    _ = await manager.send_to_session(
                        str(self.agent_id),
                        self.conv_id,
                        {
                            "type": ChatEventType.DONE,
                            "role": "user",
                            "content": accepted.message.content,
                            "message_id": str(accepted.message.id),
                            "client_submission_id": str(submission.client_submission_id),
                            "session_id": self.conv_id,
                            **(
                                {"attachments": [ref.as_dict() for ref in accepted.message.attachments]}
                                if accepted.message.attachments is not None and accepted.message.attachments
                                else {}
                            ),
                        },
                        user_id=str(self.user.id),
                    )
                await _send_chat_event(self.websocket, {"type": ChatEventType.STATUS, "content": "thinking"})
            return True
        await _send_chat_event(self.websocket, rejection)
        return True

    async def _handle_onboarding_trigger_guard(self) -> bool:
        """Returns True if the onboarding trigger was ignored (already started)."""
        if await is_onboarded(self.agent_id, self.user.id):
            logger.info("[WS] Onboarding trigger ignored - pair already onboarded")
            await _send_chat_event(
                self.websocket,
                {
                    "type": ChatEventType.ONBOARDED,
                    "agent_id": str(self.agent_id),
                },
            )
            return True
        return False

    async def _check_quotas(self) -> bool:
        """Check expiry before opening the turn transaction, including expiry side effects."""
        try:
            await check_agent_expired(self.agent_id)
            return True
        except AgentExpired as ae:
            await _send_chat_event(
                self.websocket, {"type": ChatEventType.TURN_REJECTED, "reason": "agent_expired", "content": ae.message}
            )
            return False

    async def _save_user_message(self, content: str, display_content: str, file_name: str, is_onboarding_trigger: bool):
        """Saves user message to the database and updates session title/time."""
        has_image_marker = "[image_data:" in content
        if has_image_marker:
            saved_content = f"[file:{file_name}]\n{content}" if file_name else content
        else:
            saved_content = display_content or content
            if file_name:
                saved_content = f"[file:{file_name}]\n{saved_content}"

        if is_onboarding_trigger:
            logger.info("[WS] Onboarding trigger - skipping user-message persistence")
            await persist_chat_message(
                agent_id=self.agent_id,
                user_id=self.user.id,
                conversation_id=self.conv_id,
                role="user",
                content="",
                skip_insert=True,
                title_if_default="Onboarding",
            )
        else:
            title_src = display_content or content
            clean_title = title_src.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
            if file_name and not clean_title:
                clean_title = f"📎 {file_name}"
            title_if_default = None
            if not self.history_messages:
                title_if_default = clean_title[:40] if clean_title else content[:40]
            await persist_chat_message(
                agent_id=self.agent_id,
                user_id=self.user.id,
                conversation_id=self.conv_id,
                role="user",
                content=saved_content,
                title_if_default=title_if_default,
            )
            logger.info("[WS] User message saved")
