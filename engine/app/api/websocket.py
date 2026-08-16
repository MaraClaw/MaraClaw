"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import re
import uuid
from datetime import UTC, datetime
from time import perf_counter
from typing import TypedDict

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from app.core.json_types import json_loads_value
from app.core.logging import logger, set_trace_id
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import load_user_from_access_token
from app.dao import agent_dao, llm_model_dao
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.dao.gateway_message_dao import gateway_message_dao
from app.dao.task_dao import task_dao
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.llm import LLMModelRecord
from app.records.user import UserRecord
from app.services.activity_logger import log_activity
from app.services.agentbay_live import detect_agentbay_env, get_browser_snapshot, get_desktop_screenshot
from app.services.chat_persist import persist_chat_message
from app.services.chat_session_service import ensure_primary_platform_session
from app.services.llm import call_llm_with_failover
from app.services.llm.base import ToolCallbackData
from app.services.llm.types import OpenAIMessage
from app.services.llm.utils import convert_chat_messages_to_llm_format, truncate_messages_with_pair_integrity
from app.services.onboarding import is_onboarded, mark_onboarding_phase, resolve_onboarding_prompt
from app.services.quota_guard import (
    AgentExpired,
    QuotaExceeded,
    check_agent_expired,
    check_conversation_quota,
    increment_agent_llm_usage,
    increment_conversation_usage,
)
from app.services.realtime import realtime_router
from app.services.task_executor import execute_task

router = APIRouter(tags=["websocket"])

MAX_LIVE_CODE_STREAM_CHARS = 120_000
LIVE_CODE_TRUNCATED_NOTICE = "\n\n[... live output truncated; execution continues ...]\n"


type WebSocketConnection = tuple[WebSocket, str | None, str | None]
type RealtimeMessage = dict[str, object]


def _json_object_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _payload_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


class ToolLivePreview(TypedDict, total=False):
    env: str
    screenshot_url: str
    output: str


class WorkspaceActivity(TypedDict):
    action: str
    path: str
    tool: str
    ok: bool
    pendingApproval: bool


class WebSocketToolCall(ToolCallbackData, total=False):
    live_preview: ToolLivePreview
    workspace_activity: WorkspaceActivity


def extract_partial_content(args_str: str) -> str:
    """Extract the string value of the 'content' field from a partial JSON tool-arguments string.

    When the LLM streams the finish tool call, arguments arrive as an
    incrementally-growing JSON fragment like '{"content": "hello \\\\n wor'.
    This function parses what is available so far, correctly handling JSON
    escape sequences (\\n, \\", \\\\, \\\\uXXXX, etc.) even when the string is
    truncated mid-escape.
    """
    import re as _re

    s = args_str.strip()
    match = _re.search(r'"content"\s*:\s*"', s)
    if not match:
        return ""

    start_idx = match.end()
    val_chars: list[str] = []
    escaped = False
    i = start_idx
    n = len(s)
    while i < n:
        c = s[i]
        if escaped:
            if c == "n":
                val_chars.append("\n")
            elif c == "t":
                val_chars.append("\t")
            elif c == "r":
                val_chars.append("\r")
            elif c == "b":
                val_chars.append("\b")
            elif c == "f":
                val_chars.append("\f")
            elif c == '"':
                val_chars.append('"')
            elif c == "\\":
                val_chars.append("\\")
            elif c == "/":
                val_chars.append("/")
            elif c == "u":
                if i + 4 < n:
                    try:
                        hex_val = int(s[i + 1 : i + 5], 16)
                        val_chars.append(chr(hex_val))
                        i += 4
                    except ValueError:
                        val_chars.append("\\")
                        val_chars.append("u")
                else:
                    # Incomplete \uXXXX - wait for more data
                    val_chars.append("\\")
                    val_chars.append("u")
            else:
                val_chars.append(c)
            escaped = False
        else:
            if c == "\\":
                escaped = True
            elif c == '"':
                # End of the JSON string value
                break
            else:
                val_chars.append(c)
        i += 1
    return "".join(val_chars)


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

    async def send_to_session(self, agent_id: str, session_id: str, message: RealtimeMessage):
        """Send message only to WebSocket connections matching the given session_id."""
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            session_id=session_id,
        )

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
    """Manages connection lifecycle, message polling, LLM orchestration, and persistence for a single user-agent session."""

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
        self.llm_model: LLMModelRecord | None = None
        self.secondary_llm_model: LLMModelRecord | None = None
        self.fallback_llm_model: LLMModelRecord | None = None
        self._honor_override = False
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

            # Load models
            await self._load_models(None)

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

    async def _load_models(self, db: object | None):
        """Loads primary, secondary, and fallback models for the agent."""
        del db
        from app.services.llm.router import load_agent_model_bundle

        bundle = await load_agent_model_bundle(self.agent)
        self.llm_model = bundle.primary if bundle.primary and bundle.primary.enabled else None
        self.secondary_llm_model = bundle.secondary if bundle.secondary and bundle.secondary.enabled else None
        self.fallback_llm_model = bundle.fallback if bundle.fallback and bundle.fallback.enabled else None
        if self.llm_model:
            logger.info(f"[WS] Primary model loaded: {self.llm_model.model}")
        if self.secondary_llm_model:
            logger.info(f"[WS] Secondary model loaded: {self.secondary_llm_model.model}")
        if self.fallback_llm_model:
            logger.info(f"[WS] Fallback model loaded: {self.fallback_llm_model.model}")

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
            override_raw = data.get("model_id")
            override_model_id = override_raw if isinstance(override_raw, str) else None
            is_onboarding_trigger = data.get("kind") == "onboarding_trigger"
            logger.info(f"[WS] Received: {content[:50]}" + (" [onboarding]" if is_onboarding_trigger else ""))

            if not content and not is_onboarding_trigger:
                continue

            if is_onboarding_trigger:
                if await self._handle_onboarding_trigger_guard():
                    continue
                content = "Please begin the onboarding."

            self.current_user_text = content
            effective_llm_model = await self._resolve_effective_model(override_model_id)

            # Quota Checks
            if not await self._check_quotas():
                continue

            # Add user message to in-memory context
            self.conversation.append({"role": "user", "content": content})

            # Save user message to DB
            await self._save_user_message(content, display_content, file_name, is_onboarding_trigger)

            # OpenClaw routing check
            if self.agent_type == "openclaw":
                await self._route_openclaw(content)
                continue

            # Detect task creation intent
            task_match = re.search(
                r"(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\\s]*(.+)",
                content,
                re.IGNORECASE,
            )

            # Invoke LLM and stream response
            if effective_llm_model:
                assistant_response, thinking_content, queued_messages = await self._run_llm_and_stream(
                    effective_llm_model, is_onboarding_trigger
                )
            else:
                assistant_response = (
                    f"⚠️ {self.agent_name} has no LLM model configured. "
                    + "Please select a model in the agent's Settings tab."
                )
                thinking_content = []
                queued_messages = []

            # If task creation detected, create a real Task record
            if task_match:
                assistant_response = await self._create_task_record(task_match.group(1).strip(), assistant_response)

            # Add assistant response to in-memory conversation
            self.conversation.append({"role": "assistant", "content": assistant_response})

            # Save assistant reply
            await self._save_assistant_reply(assistant_response, thinking_content)

            # Final 'done' packet
            await self.websocket.send_json({"type": "done", "role": "assistant", "content": assistant_response})

            # Re-process any queued messages (if user sent something during generation)
            for _ in queued_messages:
                pass

    async def _handle_onboarding_trigger_guard(self) -> bool:
        """Returns True if the onboarding trigger was ignored (already onboarded)."""
        if await is_onboarded(None, self.agent_id, self.user.id):
            logger.info("[WS] Onboarding trigger ignored - pair already onboarded")
            await self.websocket.send_json(
                {
                    "type": "onboarded",
                    "agent_id": str(self.agent_id),
                }
            )
            return True
        return False

    async def _resolve_effective_model(self, override_model_id: str | None) -> LLMModelRecord | None:
        """Reloads model config and resolves effective model (taking overrides into account)."""
        _agent_cur = await agent_dao.get(self.agent_id)
        if _agent_cur:
            from app.services.llm.router import load_agent_model_bundle

            bundle = await load_agent_model_bundle(_agent_cur)
            self.llm_model = bundle.primary if bundle.primary and bundle.primary.enabled else None
            self.secondary_llm_model = (
                bundle.secondary if bundle.secondary and bundle.secondary.enabled else None
            )
            self.fallback_llm_model = bundle.fallback if bundle.fallback and bundle.fallback.enabled else None

        self._honor_override = False
        effective_llm_model = self.llm_model or self.secondary_llm_model or self.fallback_llm_model
        if override_model_id:
            try:
                _ovr_uuid = uuid.UUID(str(override_model_id))
                _ovr = await llm_model_dao.get(_ovr_uuid)
                agent_tenant = getattr(self.agent, "tenant_id", None)
                if (
                    _ovr
                    and _ovr.enabled
                    and (
                        _ovr.tenant_id is None
                        or (agent_tenant is not None and _ovr.tenant_id == agent_tenant)
                    )
                ):
                    effective_llm_model = _ovr
                    self._honor_override = True
                else:
                    logger.warning(
                        f"[WS] model override {override_model_id} rejected (missing/disabled/tenant mismatch)"
                    )
            except ValueError, TypeError:
                logger.warning(f"[WS] model override {override_model_id!r} is not a valid UUID")

        return effective_llm_model

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
        _ = await gateway_message_dao.create(
            obj_in={
                "agent_id": self.agent_id,
                "sender_user_id": self.user.id,
                "conversation_id": self.conv_id,
                "content": content,
                "status": "pending",
            }
        )
        logger.info("[WS] OpenClaw: message queued for gateway poll")
        await self.websocket.send_json(
            {
                "type": "done",
                "role": "assistant",
                "content": "Message forwarded to OpenClaw agent. Waiting for response...",
            }
        )

    async def _run_llm_and_stream(
        self, effective_llm_model: LLMModelRecord, is_onboarding_trigger: bool
    ) -> tuple[str, list[str], list[RealtimeMessage]]:
        """Calls the LLM and streams response chunks to WebSocket."""
        start_gen = perf_counter()
        try:
            logger.info(f"[WS] Calling LLM {effective_llm_model.model} (streaming)...")

            # Accumulate partial content for abort handling
            partial_chunks: list[str] = []
            # Track how many characters of finish-tool content have been streamed
            finish_content_sent_len = 0

            # Set inside _call_with_failover when an onboarding prompt was injected
            needs_onboarding_mark = False
            onboarding_target_phase = "completed"
            onboarding_mark_done = False

            async def maybe_mark_onboarding_progress():
                nonlocal onboarding_mark_done
                if needs_onboarding_mark and not onboarding_mark_done:
                    onboarding_mark_done = True
                    try:
                        await mark_onboarding_phase(
                            None,
                            self.agent_id,
                            self.user.id,
                            onboarding_target_phase,
                        )
                        # Tell the frontend to refresh its cached agent record
                        await self.websocket.send_json(
                            {
                                "type": "onboarded",
                                "agent_id": str(self.agent_id),
                            }
                        )
                    except Exception as _ob_err:
                        logger.warning(f"[WS] mark_onboarded failed (non-fatal): {_ob_err}")

            async def stream_to_ws(text: str):
                """Send each chunk to client in real-time."""
                partial_chunks.append(text)
                await self.websocket.send_json({"type": "chunk", "content": text})
                await maybe_mark_onboarding_progress()

            async def tool_call_to_ws(data: ToolCallbackData):
                """Send tool call info to client and persist completed ones."""
                tool_call = WebSocketToolCall(**data)
                if tool_call.get("status") in {"running", "done"}:
                    await maybe_mark_onboarding_progress()
                if tool_call.get("status") == "done":
                    # Inject Live Preview & Workspace Activities
                    await self._inject_live_preview_and_workspace_metadata(tool_call)

                await self.websocket.send_json({"type": "tool_call", **tool_call})

                # Save completed tool calls to DB so they persist in chat history
                if tool_call.get("status") == "done":
                    await self._save_completed_tool_call_to_db(tool_call)

            # Track thinking content for storage
            thinking_content: list[str] = []

            async def thinking_to_ws(text: str):
                """Send thinking chunks to client for collapsible display."""
                thinking_content.append(text)
                await self.websocket.send_json({"type": "thinking", "content": text})

            _workspace_draft_cache: dict[str, str] = {}

            async def tool_delta_to_ws(data: ToolCallbackData):
                """Stream workspace file-operation drafts while tool args are still arriving."""
                nonlocal finish_content_sent_len
                tool_name = data.get("name", "")

                # Stream finish tool content as real-time chunks
                if tool_name == "finish":
                    raw_args = data.get("arguments", "")
                    if isinstance(raw_args, str) and raw_args:
                        current_content = extract_partial_content(raw_args)
                        if len(current_content) > finish_content_sent_len:
                            delta = current_content[finish_content_sent_len:]
                            finish_content_sent_len = len(current_content)
                            await stream_to_ws(delta)
                    return

                _ws_tools = {
                    "write_file",
                    "edit_file",
                    "move_file",
                    "delete_file",
                    "convert_markdown_to_docx",
                    "convert_csv_to_xlsx",
                    "convert_markdown_to_pdf",
                    "convert_html_to_pdf",
                    "convert_html_to_pptx",
                }
                if tool_name not in _ws_tools:
                    return

                raw_args = data.get("arguments", "")

                draft_id = str(data.get("id") or f"draft-{data.get('index', 0)}")
                if _workspace_draft_cache.get(draft_id) == raw_args:
                    return
                _workspace_draft_cache[draft_id] = raw_args

                await self.websocket.send_json(
                    {
                        "type": "workspace_draft",
                        "id": draft_id,
                        "index": data.get("index", 0),
                        "name": tool_name,
                        "arguments": raw_args,
                    }
                )

            # Run call_llm_with_failover as a cancellable task
            async def _call_with_failover():
                nonlocal needs_onboarding_mark, onboarding_target_phase

                async def _on_failover(reason: str):
                    await self.websocket.send_json({"type": "info", "content": reason})

                _truncated = truncate_messages_with_pair_integrity(self.conversation, self.ctx_size)

                # Resolve onboarding prompt
                skip_tools_for_greeting = False
                try:
                    _onb = await resolve_onboarding_prompt(
                        None,
                        self.agent,
                        self.user.id,
                        user_name=self.user_display_name,
                        user_locale=self.lang,
                    )
                    if _onb:
                        onboarding_message: OpenAIMessage = {"role": "system", "content": _onb.prompt}
                        _truncated: list[OpenAIMessage] = [onboarding_message, *_truncated]
                        if _onb.lock_on_first_chunk:
                            needs_onboarding_mark = True
                            onboarding_target_phase = _onb.target_phase
                        if _onb.is_greeting_turn:
                            skip_tools_for_greeting = True
                except Exception as _onb_err:
                    logger.warning(f"[WS] Onboarding prompt resolve failed (non-fatal): {_onb_err}")

                live_code_chars_sent = 0
                live_code_truncated_sent = False

                async def code_output_to_ws(text: str, label: str = "stdout"):
                    """Stream execute_code output chunks to the frontend live panel in real-time."""
                    nonlocal live_code_chars_sent, live_code_truncated_sent
                    try:
                        remaining = MAX_LIVE_CODE_STREAM_CHARS - live_code_chars_sent
                        if remaining <= 0:
                            if not live_code_truncated_sent:
                                live_code_truncated_sent = True
                                await self.websocket.send_json(
                                    {
                                        "type": "agentbay_live",
                                        "env": "code",
                                        "output": LIVE_CODE_TRUNCATED_NOTICE,
                                        "stream": label,
                                    }
                                )
                            return

                        output = text[:remaining]
                        live_code_chars_sent += len(output)
                        await self.websocket.send_json(
                            {
                                "type": "agentbay_live",
                                "env": "code",
                                "output": output,
                                "stream": label,
                            }
                        )
                    except Exception as error:
                        logger.warning(f"[WS] Failed to stream code output: {error}")

                from app.services.llm.router import select_turn_model
                from app.services.llm.turn import ModelBundle, TurnContext

                selected_model = effective_llm_model
                failover_model = self.fallback_llm_model
                if not self._honor_override:
                    choice = await select_turn_model(
                        ModelBundle(
                            primary=self.llm_model,
                            secondary=self.secondary_llm_model,
                            fallback=self.fallback_llm_model,
                        ),
                        user_text=getattr(self, "current_user_text", "") or "",
                        history=_truncated,
                        skip_tools=skip_tools_for_greeting,
                        agent_id=self.agent_id,
                    )
                    if choice.model is not None:
                        selected_model = choice.model
                        failover_model = choice.failover_model

                return await call_llm_with_failover(
                    primary_model=selected_model,
                    fallback_model=failover_model,
                    messages=_truncated,
                    agent_name=self.agent_name,
                    role_description=self.role_description,
                    agent_id=self.agent_id,
                    user_id=self.user.id,
                    session_id=self.conv_id,
                    turn=TurnContext(
                        agent=self.agent,
                        primary_model=self.llm_model,
                        secondary_model=self.secondary_llm_model,
                        fallback_model=self.fallback_llm_model,
                        selected_model=selected_model,
                        user=self.user,
                    ),
                    on_chunk=stream_to_ws,
                    on_tool_call=tool_call_to_ws,
                    on_tool_delta=tool_delta_to_ws,
                    on_thinking=thinking_to_ws,
                    supports_vision=getattr(selected_model, "supports_vision", False),
                    on_failover=_on_failover,
                    skip_tools=skip_tools_for_greeting,
                    on_code_output=code_output_to_ws,
                )

            llm_task = asyncio.create_task(_call_with_failover())

            # Listen for abort while LLM is running
            aborted = False
            queued_messages: list[RealtimeMessage] = []
            while not llm_task.done():
                try:
                    msg = _json_object_payload(
                        json_loads_value(await asyncio.wait_for(self.websocket.receive_text(), timeout=0.5))
                    )
                    if msg.get("type") == "abort":
                        logger.info("[WS] Abort received, cancelling LLM task")
                        _ = llm_task.cancel()
                        aborted = True
                        break
                    queued_messages.append(msg)
                except TimeoutError:
                    continue
                except WebSocketDisconnect:
                    _ = llm_task.cancel()
                    raise

            if aborted:
                try:
                    await llm_task
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task and current_task.cancelling():
                        raise
                    logger.debug("[WS] LLM task cancelled after user abort")
                except Exception as error:
                    logger.warning(f"[WS] LLM task failed while aborting: {error}")
                partial_text = "".join(partial_chunks).strip()
                assistant_response = (
                    (partial_text + "\n\n*[Generation stopped]*") if partial_text else "*[Generation stopped]*"
                )
                logger.info(f"[WS] LLM aborted, partial: {assistant_response[:80]}")
            else:
                assistant_response = await llm_task
                logger.info(f"[WS] LLM response: {assistant_response[:80]}")

            # Raise error on prefix for failover matching
            _llm_error_prefixes = ("[LLM Error]", "[LLM call error]", "[Error]")
            if (
                not aborted
                and assistant_response
                and any(assistant_response.startswith(p) for p in _llm_error_prefixes)
            ):
                raise RuntimeError(assistant_response)

            # Post-success actions (last_active_at, quota usage increments, activity logs)
            await self._update_activity_and_quota(assistant_response)

            return assistant_response, thinking_content, queued_messages

        except WebSocketDisconnect:
            raise
        except Exception as e:
            gen_duration = perf_counter() - start_gen
            logger.exception(f"[WS] LLM error after {gen_duration:.3f}s: {e}")
            return f"[LLM call error] {str(e)[:200]}", [], []

    async def _inject_live_preview_and_workspace_metadata(self, data: WebSocketToolCall):
        """Injects live previews and workspace panel activity tracking into tool results."""
        try:
            tool_name = data.get("name", "")
            env = detect_agentbay_env(tool_name)
            if env == "desktop":
                b64_url = await get_desktop_screenshot(self.agent_id, session_id=self.conv_id)
                if b64_url:
                    data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                    logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
            elif env == "browser":
                b64_url = await get_browser_snapshot(self.agent_id, session_id=self.conv_id)
                if b64_url:
                    data["live_preview"] = {"env": env, "screenshot_url": b64_url}
                    logger.info(f"[WS][LivePreview] Embedded {env} base64 in tool_call")
            elif env == "code":
                tool_result = data.get("result", "") or ""
                data["live_preview"] = {"env": "code", "output": tool_result[:5000]}
        except Exception as _lp_err:
            logger.warning(f"[WS][LivePreview] Embed failed: {_lp_err}")

        _workspace_tool_actions = {
            "write_file": "write",
            "edit_file": "edit",
            "move_file": "move",
            "delete_file": "delete",
            "convert_markdown_to_docx": "convert",
            "convert_csv_to_xlsx": "convert",
            "convert_markdown_to_pdf": "convert",
            "convert_html_to_pdf": "convert",
            "convert_html_to_pptx": "convert",
        }
        _done_tool_name = data.get("name", "")
        if _done_tool_name in _workspace_tool_actions:
            workspace_args = data.get("args", {})
            workspace_path_value = (
                workspace_args.get("output_path")
                or workspace_args.get("destination_path")
                or workspace_args.get("path")
                or ""
            )
            workspace_path = workspace_path_value if isinstance(workspace_path_value, str) else ""
            _ws_result = str(data.get("result") or "")
            _pending_approval = "requires approval" in _ws_result.lower()
            data["workspace_activity"] = {
                "action": _workspace_tool_actions[_done_tool_name],
                "path": workspace_path,
                "tool": _done_tool_name,
                "ok": not _pending_approval,
                "pendingApproval": _pending_approval,
            }
            logger.info(f"[WS][Workspace] activity: {_done_tool_name} → {workspace_path}")

    async def _save_completed_tool_call_to_db(self, data: ToolCallbackData):
        """Persist completed tool calls in ChatMessage DB logs."""
        try:
            from app.services.chat_session_service import save_tool_call_log

            await save_tool_call_log(
                agent_id=self.agent_id,
                user_id=self.user.id,
                conversation_id=self.conv_id,
                tool_name=data.get("name", ""),
                arguments=data.get("args"),
                result=(data.get("result") or "")[:500],
                status="done",
                tool_call_id=data.get("call_id"),
                reasoning_content=data.get("reasoning_content"),
            )
            _ = await maybe_mark_session_read_for_active_viewer(
                None,
                agent_id=self.agent_id,
                session_id=self.conv_id,
                user_id=self.user.id,
            )
        except Exception as _tc_err:
            logger.warning(f"[WS] Failed to save tool_call: {_tc_err}")

    async def _update_activity_and_quota(self, assistant_response: str):
        """Update conversation/agent LLM usage and log activity."""
        try:
            await increment_conversation_usage(self.user.id)
            await increment_agent_llm_usage(self.agent_id)
        except Exception as error:
            logger.warning(f"[WS] Failed to update usage counters: {error}")

        try:
            user_text = getattr(self, "current_user_text", "")
            await log_activity(
                self.agent_id,
                "chat_reply",
                f"Replied to web chat: {assistant_response[:80]}",
                detail={"channel": "web", "user_text": user_text[:200], "reply": assistant_response[:500]},
            )
        except Exception as e:
            logger.warning(f"[WS] Failed to log activity: {e}")

    async def _create_task_record(self, task_title: str, assistant_response: str) -> str:
        """Creates a background execution task from task matching."""
        if not task_title:
            return assistant_response
        try:
            task = await task_dao.create(
                obj_in={
                    "agent_id": self.agent_id,
                    "title": task_title,
                    "created_by": self.user.id,
                    "status": "pending",
                    "priority": "medium",
                    "type": "todo",
                    "assignee": "self",
                }
            )
            logger.info(f"[WS] Task created: {task.id}")
            from app.api.background_tasks import schedule_background_task

            _ = schedule_background_task(execute_task(task.id, self.agent_id), "execute web task")
            assistant_response += f"\n\n📋 Task synced to task board: [{task_title}]"
        except Exception as te:
            logger.error(f"[WS] Task creation failed: {te}")
        return assistant_response

    async def _save_assistant_reply(self, assistant_response: str, thinking_content: list[str]):
        """Saves assistant reply to DB."""
        await persist_chat_message(
            agent_id=self.agent_id,
            user_id=self.user.id,
            conversation_id=self.conv_id,
            role="assistant",
            content=assistant_response,
            thinking="".join(thinking_content) if thinking_content else None,
            touch_last_active=True,
            agent=self.agent,
        )
        _ = await maybe_mark_session_read_for_active_viewer(
            None,
            agent_id=self.agent_id,
            session_id=self.conv_id,
            user_id=self.user.id,
        )
        logger.info("[WS] Assistant message saved")
