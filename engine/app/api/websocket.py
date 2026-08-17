"""WebSocket chat endpoint for real-time agent conversations."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.core.json_types import json_loads_value
from app.core.logging import logger, set_trace_id
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import load_user_from_access_token
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
from app.services.chat_persist import persist_chat_message
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.llm.types import OpenAIMessage
from app.services.llm.utils import convert_chat_messages_to_llm_format
from app.services.onboarding import is_onboarded, try_begin_onboarding_greeting
from app.services.quota_guard import (
    AgentExpired,
    QuotaExceeded,
    check_agent_expired,
    check_conversation_quota,
)
from app.services.realtime import realtime_router

router = APIRouter(tags=["websocket"])


type WebSocketConnection = tuple[WebSocket, str | None, str | None]
type RealtimeMessage = dict[str, object]


def _json_object_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _payload_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


class ConnectionManager:
    """Manage WebSocket connections per agent."""

    def __init__(self):
        # agent_id_str -> list of (WebSocket, session_id_str | None, user_id_str | None)
        self.active_connections: dict[str, list[WebSocketConnection]] = {}

    async def connect(
        self, agent_id: str, websocket: WebSocket, session_id: str | None = None, user_id: str | None = None
    ):
        if agent_id not in self.active_connections:
            self.active_connections[agent_id] = []
        self.active_connections[agent_id].append((websocket, session_id, user_id))
        _ = await realtime_router.register_connection(
            agent_id=agent_id,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
        )

    async def disconnect(self, agent_id: str, websocket: WebSocket):
        if agent_id in self.active_connections:
            self.active_connections[agent_id] = [
                (ws, sid, uid) for ws, sid, uid in self.active_connections[agent_id] if ws != websocket
            ]
        await realtime_router.unregister_connection(agent_id=agent_id, websocket=websocket)

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
        if agent_id not in self.active_connections:
            return
        for ws, sid, uid in list(self.active_connections[agent_id]):
            if session_id is not None and sid != session_id:
                continue
            if user_id is not None and uid != user_id:
                continue
            try:
                await ws.send_json(payload)
            except Exception as error:
                logger.warning(f"[WS] Failed to send message to connected client: {error}")

    async def send_message(self, agent_id: str, message: RealtimeMessage):
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
        )

    async def send_to_session(
        self,
        agent_id: str,
        session_id: str,
        message: RealtimeMessage,
        user_id: str | None = None,
    ):
        """Send to sockets on ``session_id``, or the user's live sockets if none match."""
        wanted = (session_id or "").strip().lower()
        local = self._local_connections(agent_id)
        session_hits = [(ws, sid, uid) for ws, sid, uid in local if (sid or "").strip().lower() == wanted]
        if session_hits:
            await realtime_router.route_message(
                agent_id=agent_id,
                message=message,
                local_connections=session_hits,
                session_id=None,
            )
            return
        if user_id:
            logger.info(
                "[WS] no socket for session {} agent={}; delivering to user {}",
                session_id,
                agent_id,
                user_id,
            )
            await realtime_router.route_message(
                agent_id=agent_id,
                message=message,
                local_connections=local,
                user_id=user_id,
            )
            return
        logger.warning("[WS] dropped live reply; no socket agent={} session={}", agent_id, session_id)

    async def send_to_user(self, agent_id: str, user_id: str, message: RealtimeMessage):
        """Send message to all live WebSocket sessions of a given platform user for an agent."""
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            user_id=user_id,
        )

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        """Return distinct session IDs for all active WS connections of an agent."""
        return await realtime_router.get_active_session_ids(agent_id)

    async def is_user_viewing_session(self, agent_id: str, session_id: str, user_id: str) -> bool:
        """Return True if the given platform user currently has this exact session open."""
        return await realtime_router.is_user_viewing_session(
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )


manager = ConnectionManager()


async def maybe_mark_session_read_for_active_viewer(
    db: object | None,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: uuid.UUID,
) -> bool:
    """Advance last_read_at_by_user if the owner is actively viewing this exact session."""
    del db
    if not await manager.is_user_viewing_session(str(agent_id), session_id, str(user_id)):
        return False

    try:
        sid = uuid.UUID(session_id)
    except ValueError, TypeError:
        return False
    session = await chat_session_dao.get(sid)
    if not session:
        return False

    _ = await chat_session_dao.update(db_obj=session, obj_in={"last_read_at_by_user": datetime.now(UTC)})
    return True


@router.websocket("/ws/chat/{agent_id}")
async def websocket_chat(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    token: str = Query(...),
    session_id: str = Query(None),
    lang: str = Query("en"),
):
    """WebSocket endpoint for real-time chat with an agent."""
    handler = WebSocketChatHandler(websocket, agent_id, token, session_id, lang)
    await handler.run()


class WebSocketChatHandler:
    """Manages connection lifecycle, inbound persistence, and OpenClaw enqueue for a user-agent session."""

    user: UserRecord
    agent: AgentRecord
    conv_id: str

    def __init__(
        self,
        websocket: WebSocket,
        agent_id: uuid.UUID,
        token: str,
        session_id: str | None = None,
        lang: str = "en",
    ):
        self.websocket: WebSocket = websocket
        self.agent_id: uuid.UUID = agent_id
        self.token: str = token
        self.session_id_param: str | None = session_id
        self.lang: str = lang

        # State fields initialized during setup
        self.agent_name: str = ""
        self.agent_type: str = ""
        self.role_description: str = ""
        self.welcome_message: str = ""
        self.ctx_size: int = 100
        self.user_display_name: str = ""
        self.history_messages: list[ChatMessageRecord] = []
        self.conversation: list[OpenAIMessage] = []
        self.current_user_text: str = ""

    async def run(self):
        """Main entry point for handling the lifecycle of the WebSocket connection."""
        try:
            # 1. Setup session (Authentication, permissions, loading models, history, etc.)
            success = await self.setup()
            if not success:
                return

            # 2. Start the message receiving and processing loop
            await self.message_loop()

        except WebSocketDisconnect:
            logger.info(f"[WS] Client disconnected: {getattr(self.user, 'id', 'unknown')}")
            await manager.disconnect(str(self.agent_id), self.websocket)
        except Exception as e:
            logger.exception(f"[WS] Unexpected error: {e}")
            await manager.disconnect(str(self.agent_id), self.websocket)

    async def setup(self) -> bool:
        """Accepts connection, authenticates user, verifies agent access, loads models, resolves session & history."""
        # Accept immediately so browser sees onopen without waiting for DB setup
        await self.websocket.accept()

        # Authenticate (identity loaded; must_change_password enforced like REST)
        try:
            user = await load_user_from_access_token(
                self.token,
                require_active=True,
                enforce_password_change=True,
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("must_change_password"):
                content: str = detail.get("message") or "Password change required before continuing."
            else:
                content = "Authentication failed"
            await self.websocket.send_json({"type": "error", "content": content})
            await self.websocket.close(code=4001)
            return False
        except Exception:
            await self.websocket.send_json({"type": "error", "content": "Authentication failed"})
            await self.websocket.close(code=4001)
            return False

        try:
            self.user = user

            logger.info(f"[WS] Checking agent access for {self.agent_id}")
            self.agent, _ = await check_agent_access(self.user, self.agent_id)
            if is_agent_expired(self.agent):
                await self.websocket.send_json(
                    {
                        "type": "error",
                        "content": "This Agent has expired and is off duty. Please contact your admin to extend its service.",
                    }
                )
                await self.websocket.close(code=4003)
                return False

            self.agent_name = self.agent.name
            self.agent_type = self.agent.agent_type or ""
            self.role_description = self.agent.role_description or ""
            self.welcome_message = self.agent.welcome_message or ""
            self.ctx_size = self.agent.context_window_size or 100
            self.user_display_name = (self.user.display_name or "").strip() or "there"
            logger.info(
                f"[WS] Agent: {self.agent_name}, type: {self.agent_type}, model_id: {self.agent.primary_model_id}, ctx: {self.ctx_size}"
            )

            # Resolve or create chat session
            conv_id = await self._resolve_chat_session(None, self.user.id)
            if not conv_id:
                return False
            self.conv_id = conv_id

            # Load history messages
            await self._load_history(None)

        except Exception as e:
            logger.exception(f"[WS] Setup error: {e}")
            await self.websocket.send_json({"type": "error", "content": "Setup failed"})
            await self.websocket.close(code=4002)
            return False

        # Connect connection manager
        agent_id_str = str(self.agent_id)
        await manager.connect(agent_id_str, self.websocket, self.conv_id, str(self.user.id))
        logger.info(f"[WS] Ready! Agent={self.agent_name}")

        # Send session_id to frontend
        await self.websocket.send_json({"type": "connected", "session_id": self.conv_id})

        # Build conversation context
        self.conversation = self._build_conversation_context()

        return True

    async def _resolve_chat_session(self, db: object | None, user_id: uuid.UUID) -> str | None:
        """Resolves existing session or creates a new one."""
        del db
        conv_id = self.session_id_param
        if conv_id:
            try:
                _sid = uuid.UUID(conv_id)
            except ValueError, TypeError:
                conv_id = None
                _existing = None
            else:
                _existing = await chat_session_dao.get_for_agent(_sid, self.agent_id)
                if not _existing:
                    conv_id = None
                elif _existing.source_channel != "agent" and str(_existing.user_id) != str(user_id):
                    await self.websocket.send_json({"type": "error", "content": "Not authorized for this session"})
                    await self.websocket.close(code=4003)
                    return None
        if not conv_id:
            _latest = await chat_session_dao.get_primary_platform(agent_id=self.agent_id, user_id=user_id)
            if _latest:
                conv_id = str(_latest.id)
            else:
                _new_session = await ensure_primary_platform_session(None, self.agent_id, user_id)
                conv_id = str(_new_session.id)
                logger.info(f"[WS] Selected primary session {conv_id}")
        return conv_id

    async def _load_history(self, db: object | None):
        """Loads and prepares history messages for the conversation."""
        del db
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
            logger.warning(f"[WS] History load failed (non-fatal): {e}")

    def _build_conversation_context(self) -> list[OpenAIMessage]:
        """Translates historical ChatMessages to LLM inputs."""
        return convert_chat_messages_to_llm_format(self.history_messages)

    async def message_loop(self):
        """Core message processing loop."""
        # Send welcome message on new session (no history)
        if self.welcome_message and not self.history_messages:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": self.welcome_message})

        while True:
            data = _json_object_payload(json_loads_value(await self.websocket.receive_text()))

            # Set a unique trace ID for this specific message processing.
            trace_id = str(uuid.uuid4())[:12]
            set_trace_id(trace_id)

            content = _payload_str(data.get("content", ""))
            display_content = _payload_str(data.get("display_content", ""))
            file_name = _payload_str(data.get("file_name", ""))
            is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
            logger.info(f"[WS] Received: {content[:50]}" + (" [onboarding]" if is_onboarding_trigger else ""))

            if not content and not is_onboarding_trigger:
                continue

            if is_onboarding_trigger:
                if await self._handle_onboarding_trigger_guard():
                    continue
                content = "Please begin the onboarding."

            self.current_user_text = content

            # Quota Checks
            if not await self._check_quotas():
                continue

            # Add user message to in-memory context
            self.conversation.append({"role": "user", "content": content})

            # Save user message to DB
            await self._save_user_message(content, display_content, file_name, is_onboarding_trigger)

            await self._route_openclaw(content)
            continue

    async def _handle_onboarding_trigger_guard(self) -> bool:
        """Returns True if the onboarding trigger was ignored (already started)."""
        if await is_onboarded(None, self.agent_id, self.user.id):
            logger.info("[WS] Onboarding trigger ignored - pair already onboarded")
            await self.websocket.send_json(
                {
                    "type": "onboarded",
                    "agent_id": str(self.agent_id),
                }
            )
            return True
        claimed = await try_begin_onboarding_greeting(None, self.agent_id, self.user.id)
        if not claimed:
            logger.info("[WS] Onboarding trigger ignored - greeting already claimed")
            await self.websocket.send_json(
                {
                    "type": "onboarded",
                    "agent_id": str(self.agent_id),
                }
            )
            return True
        return False

    async def _check_quotas(self) -> bool:
        """Checks conversation and agent LLM quotas. Sends message and returns False if exceeded."""
        try:
            await check_conversation_quota(self.user.id)
            await check_agent_expired(self.agent_id)
            return True
        except QuotaExceeded as qe:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {qe.message}"})
            return False
        except AgentExpired as ae:
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": f"⚠️ {ae.message}"})
            return False

    async def _save_user_message(self, content: str, display_content: str, file_name: str, is_onboarding_trigger: bool):
        """Saves user message to the database and updates session title/time."""
        has_image_marker = "[image_data:" in content
        if has_image_marker:
            saved_content = f"[file:{file_name}]\n{content}" if file_name else content
        else:
            saved_content = display_content if display_content else content
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
            title_src = display_content if display_content else content
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

    async def _route_openclaw(self, content: str):
        """Enqueues message for OpenClaw edge node poll."""
        from app.services.openclaw_routing import NoCompanyModelError, enqueue_openclaw_message

        await self.websocket.send_json(
            {
                "type": "info",
                "content": "Message forwarded to OpenClaw agent. Waiting for response...",
            }
        )
        try:
            _ = await enqueue_openclaw_message(
                agent=self.agent,
                content=content,
                sender_user_id=self.user.id,
                conversation_id=self.conv_id,
                history=self.conversation,
                await_wake=False,
            )
        except NoCompanyModelError as exc:
            logger.warning("[WS] OpenClaw enqueue blocked: {}", exc)
            await self.websocket.send_json({"type": "error", "content": str(exc)})
            return
        logger.info("[WS] OpenClaw: message queued for gateway poll")

