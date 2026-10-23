"""WebSocket chat endpoint for real-time agent conversations."""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.core.json_types import json_loads_value
from app.core.logging import logger, set_trace_id
from app.dao.chat_dao import chat_message_dao, chat_session_dao
from app.db.session import connection_ctx
from app.records.agent import AgentRecord
from app.records.chat import ChatMessageRecord
from app.records.user import UserRecord
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
from app.services.openclaw_routing import (
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
from app.services.realtime_runtime.chat_authorization import authorize_chat_delivery, is_shared_target
from app.services.shared_chat import is_shared_session, require_writable_session

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
                if not await authorize_chat_delivery(ws, agent_id, sid):
                    continue
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
    ) -> None:
        """Send only to this session, optionally restricting private-session recipients by user."""
        wanted = session_id.strip().lower()
        await realtime_router.route_message(
            agent_id=agent_id,
            message=message,
            local_connections=self._local_connections(agent_id),
            session_id=wanted,
            user_id=None if is_shared_target(agent_id, wanted) else user_id,
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
    if not session or is_shared_session(session):
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
):
    """WebSocket endpoint for real-time chat with an agent."""
    handler = WebSocketChatHandler(websocket, agent_id, ticket, session_id, lang)
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
    ):
        self.websocket: WebSocket = websocket
        self.agent_id: uuid.UUID = agent_id
        self.ticket: str = ticket
        self.binding: chat_tickets.ChatTicketBinding
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
            await self.websocket.close(code=4001)
        except HTTPException as exc:
            code = {401: 4001, 403: 4003, 404: 4003, 503: 1011}.get(exc.status_code, 4002)
            await self.websocket.send_json({"type": "error", "content": exc.detail})
            await self.websocket.close(code=code)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("[WS] Unexpected chat error")
            await self.websocket.close(code=1011)
        finally:
            with anyio.CancelScope(shield=True):
                await manager.disconnect(str(self.agent_id), self.websocket)

    async def setup(self) -> bool:
        self.user, self.agent, self.access_level = await chat_tickets.authorize(self.binding)
        await chat_tickets.authorized_session(self.binding)
        self.websocket.state.chat_binding = self.binding
        self.conv_id = str(self.binding.session_id)
        self.agent_name = self.agent.name
        self.agent_type = self.agent.agent_type or ""
        self.role_description = self.agent.role_description or ""
        self.welcome_message = self.agent.welcome_message or ""
        self.ctx_size = self.agent.context_window_size or 100
        self.user_display_name = (self.user.display_name or "").strip() or "there"
        await self._load_history()
        await manager.connect(str(self.agent_id), self.websocket, self.conv_id, str(self.user.id))
        await self.websocket.send_json({"type": "connected", "session_id": self.conv_id})
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

            try:
                self.user, self.agent, self.access_level = await chat_tickets.authorize(self.binding)
                await chat_tickets.authorized_session(self.binding)
            except HTTPException as exc:
                await self.websocket.send_json({"type": "error", "content": exc.detail})
                await self.websocket.close(code=4001 if exc.status_code == 401 else 4003)
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
                            "type": "turn_rejected",
                            "reason": "invalid_submission_id",
                            "content": "Submission UUID required.",
                        }
                    else:
                        rejection = {
                            "type": "turn_rejected",
                            "reason": "invalid_attachments",
                            "client_submission_id": _payload_str(data.get("client_submission_id")),
                            "content": "Attachment references must be valid agent-relative files.",
                        }
                    await self.websocket.send_json(rejection)
                    continue
                if not await self._handle_submission(submission):
                    return
                continue

            if await self._handle_onboarding_trigger_guard():
                continue
            session = await chat_session_dao.get_for_agent(self.binding.session_id, self.agent_id)
            if session is None:
                raise HTTPException(403, "Session unavailable")
            require_writable_session(session)
            if is_shared_session(session):
                await self._load_history()
                self.conversation = self._build_conversation_context()
            content = "Please begin the onboarding."

            self.current_user_text = content

            if not await self._check_quotas():
                continue

            try:
                prepared = await prepare_openclaw_message(agent=self.agent, content=content, history=self.conversation)
            except NoCompanyModelError as exc:
                logger.warning("[WS] OpenClaw preparation blocked: {}", exc)
                await self.websocket.send_json(
                    {"type": "turn_rejected", "reason": "no_company_model", "content": str(exc)}
                )
                continue
            except OpenClawUpdateGateError as gate:
                await self.websocket.send_json({"type": "turn_rejected", **gate.rejection()})
                continue

            try:
                async with connection_ctx():
                    session = await chat_session_dao.lock_for_agent(self.binding.session_id, self.agent_id)
                    if session is None:
                        raise HTTPException(403, "Session unavailable")
                    require_writable_session(session)
                    if not await try_begin_onboarding_greeting(self.agent_id, self.user.id):
                        await self.websocket.send_json({"type": "onboarded", "agent_id": str(self.agent_id)})
                        continue
                    await self._save_user_message(content, display_content, file_name, is_onboarding_trigger)
                    _ = await enqueue_prepared_openclaw_message(
                        prepared, sender_user_id=self.user.id, conversation_id=self.conv_id
                    )
            except QuotaExceeded as exc:
                await self.websocket.send_json(
                    {"type": "turn_rejected", "reason": "quota_exceeded", "content": exc.message}
                )
                continue
            except Exception:
                logger.exception("[WS] Turn transaction failed")
                await self.websocket.send_json(
                    {
                        "type": "turn_rejected",
                        "reason": "enqueue_failed",
                        "content": "Your message could not be sent. Please try again.",
                    }
                )
                continue

            self.conversation.append({"role": "user", "content": content})
            await self.websocket.send_json({"type": "status", "content": "thinking"})
            logger.info("[WS] OpenClaw: message queued for gateway poll")

    async def _handle_submission(self, submission: ChatSubmission) -> bool:
        rejection: RealtimeMessage = {
            "type": "turn_rejected",
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
        except AgentExpired as exc:
            rejection.update(reason="agent_expired", content=exc.message)
        except QuotaExceeded as exc:
            rejection.update(reason="quota_exceeded", content=exc.message)
        except HTTPException as exc:
            await self.websocket.send_json({"type": "error", "content": exc.detail})
            await self.websocket.close(code=4001 if exc.status_code == 401 else 4003)
            return False
        except Exception:
            logger.exception("[WS] Turn transaction failed")
            rejection.update(reason="enqueue_failed", content="Your message could not be sent. Please try again.")
        else:
            await self.websocket.send_json(
                {
                    "type": "turn_accepted",
                    "message_id": str(accepted.message.id),
                    "session_id": str(self.binding.session_id),
                    "client_submission_id": str(submission.client_submission_id),
                }
            )
            if not accepted.replayed:
                self.current_user_text = submission.content
                self.conversation.append({"role": "user", "content": submission.content})
                if is_shared_target(str(self.agent_id), self.conv_id):
                    await manager.send_to_session(
                        str(self.agent_id),
                        self.conv_id,
                        {
                            "type": "done",
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
                    )
                await self.websocket.send_json({"type": "status", "content": "thinking"})
            return True
        await self.websocket.send_json(rejection)
        return True

    async def _handle_onboarding_trigger_guard(self) -> bool:
        """Returns True if the onboarding trigger was ignored (already started)."""
        if await is_onboarded(self.agent_id, self.user.id):
            logger.info("[WS] Onboarding trigger ignored - pair already onboarded")
            await self.websocket.send_json(
                {
                    "type": "onboarded",
                    "agent_id": str(self.agent_id),
                }
            )
            return True
        return False

    async def _check_quotas(self) -> bool:
        """Check expiry before opening the turn transaction, including expiry side effects."""
        try:
            await check_agent_expired(self.agent_id)
            return True
        except AgentExpired as ae:
            await self.websocket.send_json({"type": "turn_rejected", "reason": "agent_expired", "content": ae.message})
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
