import json
import time
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.api import websocket as ws_api
from app.core.json_types import JsonObject, json_loads_object, json_object_from
from app.records.chat import ChatSessionRecord
from app.schemas.chat_wire import (
    CHAT_SERVER_EVENT_ADAPTER,
    ArchivedSessionErrorEvent,
    ChatEventType,
    ChatServerEvent,
    CorrelatedTurnRejectedEvent,
    InvalidSubmissionRejectedEvent,
)
from app.services.chat_submissions import ChatSubmission
from app.services.chat_tickets import ChatTicketBinding
from app.services.shared_chat import shared_session_id
from test_websocket_session_query import connected_socket
from test_websocket_turn_transaction import TurnConnection, TurnState, drive_turn

pytest_plugins = ("test_websocket_turn_transaction",)


@pytest.mark.parametrize("submission_id", [None, "not-a-uuid", ""])
async def test_invalid_submission_is_uncorrelated_when_uuid_is_unusable(
    monkeypatch: pytest.MonkeyPatch, turn: tuple[TurnState, TurnConnection], submission_id: str | None
) -> None:
    # Given an unusable submission identifier at the real socket input boundary.
    state, _raw = turn
    payload: dict[str, object] = {"content": "hello"}
    if submission_id is not None:
        payload["client_submission_id"] = submission_id
    # When the handler processes that submission.
    await drive_turn(monkeypatch, state, payload=payload)
    # Then the emitted rejection is its own ID-less wire variant.
    event = CHAT_SERVER_EVENT_ADAPTER.validate_python(state.frames[0])
    assert isinstance(event, InvalidSubmissionRejectedEvent)
    assert "client_submission_id" not in state.frames[0]
    assert not state.messages


@pytest.mark.parametrize("uppercase", [False, True])
async def test_rejection_keeps_uuid_correlation_when_attachments_are_invalid(
    monkeypatch: pytest.MonkeyPatch, turn: tuple[TurnState, TurnConnection], uppercase: bool
) -> None:
    # Given a valid submission UUID and malformed attachment references.
    state, _raw = turn
    submission_id = state.submission_id.upper() if uppercase else state.submission_id
    payload: dict[str, object] = {
        "content": "hello",
        "client_submission_id": submission_id,
        "attachments": [{"path": ""}],
    }
    # When the real handler rejects the submission.
    await drive_turn(monkeypatch, state, payload=payload)
    # Then pending-submission recovery receives exactly the original UUID.
    event = CHAT_SERVER_EVENT_ADAPTER.validate_python(state.frames[0])
    assert isinstance(event, CorrelatedTurnRejectedEvent)
    assert event.client_submission_id == UUID(state.submission_id)
    assert state.frames[0]["client_submission_id"] == submission_id
    assert event.reason == "invalid_attachments"


async def test_archived_submission_emits_structured_error_when_session_is_read_only(
    monkeypatch: pytest.MonkeyPatch, turn: tuple[TurnState, TurnConnection]
) -> None:
    # Given an archived personal session and the real submission acceptance path.
    state, _raw = turn
    socket, frames = await connected_socket()
    handler = ws_api.WebSocketChatHandler(socket, state.agent.id, "ticket")
    handler.user, handler.agent = state.user, state.agent
    archived_id = uuid4()
    assert state.user.tenant_id is not None
    handler.binding = ChatTicketBinding(
        user_id=state.user.id,
        tenant_id=state.user.tenant_id,
        agent_id=state.agent.id,
        session_id=archived_id,
        auth_deadline=int(time.time()) + 3600,
        auth_generation=0,
    )
    handler.conv_id = str(archived_id)

    async def archived_session(_session_id: UUID, _agent_id: UUID) -> ChatSessionRecord:
        return ChatSessionRecord(id=archived_id, agent_id=state.agent.id, user_id=state.user.id, source_channel="web")

    monkeypatch.setattr(ws_api.chat_session_dao, "lock_for_agent", archived_session)
    submission = ChatSubmission(client_submission_id=UUID(state.submission_id), content="hello")
    # When the actual handler encounters the writable-session guard.
    keep_open = await handler._handle_submission(submission)
    # Then the canonical replacement session stays structured, not stringified.
    frame_text = TypeAdapter[JsonObject](JsonObject).validate_python(frames[0])["text"]
    assert isinstance(frame_text, str)
    payload = json_loads_object(frame_text)
    event = CHAT_SERVER_EVENT_ADAPTER.validate_python(payload)
    assert isinstance(event, ArchivedSessionErrorEvent)
    assert event.content.session_id == shared_session_id(state.agent.id)
    assert payload == {
        "type": "error",
        "content": {"reason": "session_archived", "session_id": str(shared_session_id(state.agent.id))},
    }
    assert not keep_open
    assert frames[1]["code"] == 4003


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "turn_rejected",
            "reason": "invalid_submission_id",
            "content": "invalid",
            "client_submission_id": "bad",
        },
        {"type": "turn_rejected", "reason": "quota_exceeded", "content": "quota", "client_submission_id": "bad"},
        {"type": "turn_rejected", "reason": "invalid_attachments", "content": "attachments"},
        {"type": "turn_rejected", "reason": "submission_conflict", "content": "conflict"},
        {"type": "error", "content": {"reason": "unknown", "session_id": str(uuid4())}},
        {"type": "new_uncontracted_event"},
        {"type": "text_progress", "content": "internal discriminator is not a wire tag"},
    ],
)
def test_wire_rejects_payload_when_no_variant_matches(payload: JsonObject) -> None:
    # Given a payload that would weaken correlation or silently open the event domain.
    adapter = CHAT_SERVER_EVENT_ADAPTER
    # When parsing it at the outbound boundary, then no valid variant accepts it.
    with pytest.raises(ValidationError):
        adapter.validate_python(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "connected", "session_id": str(uuid4())},
        {
            "type": "turn_accepted",
            "client_submission_id": str(uuid4()),
            "message_id": str(uuid4()),
            "session_id": str(uuid4()),
        },
        {"type": "turn_rejected", "reason": "invalid_submission_id", "content": "invalid"},
        {"type": "turn_rejected", "reason": "no_company_model", "content": "model"},
        {"type": "error", "content": "denied"},
        {"type": "chunk", "content": "delta", "sequence": 3},
        {"type": "thinking", "content": "delta"},
        {"type": "status", "content": "thinking"},
        {"type": "info", "content": "notice"},
        {"type": "tool_call", "name": "tool", "arguments": {"nested": [1, None]}, "result": None},
        {"type": "agentbay_live", "env": "browser", "live_preview": {"screenshot_url": "/preview", "output": None}},
        {"type": "workspace_draft", "name": "note.txt", "patch": {"content": "draft"}},
        {"type": "done", "role": "assistant", "content": "reply", "attachments": [{"path": "a.txt", "name": "a"}]},
        {"type": "done", "content": "", "attachments": None},
        {
            "type": "done",
            "content": "héllo 世界 😀",
            "role": "user",
            "session_id": str(uuid4()).upper(),
            "message_id": str(uuid4()).upper(),
            "entry_id": str(uuid4()).upper(),
            "client_submission_id": str(uuid4()).upper(),
            "attachments": None,
        },
        {"type": "onboarded", "agent_id": str(uuid4())},
        {"type": "trigger_notification", "content": "notice", "triggers": ["web_message"], "session_id": str(uuid4())},
    ],
)
async def test_wire_preserves_payload_when_consumed_event_is_serialized(payload: JsonObject) -> None:
    # Given each consumed event and the additional existing notification event.
    socket, frames = await connected_socket()
    tag = payload["type"]
    assert isinstance(tag, str)
    event: dict[str, object] = {**payload, "type": ChatEventType(tag)}
    # When an enum-backed producer uses the actual outbound socket boundary.
    await ws_api._send_chat_event(socket, event)
    # Then UUID spelling, Unicode, explicit nulls, extensions and omissions keep identical wire bytes.
    assert frames == [
        {"type": "websocket.send", "text": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)}
    ]


def test_json_schema_is_exportable_when_adapter_is_built_from_named_contract() -> None:
    # Given the side-effect-free type export used by artifact generation.
    adapter = TypeAdapter[ChatServerEvent](ChatServerEvent)
    # When generating its JSON Schema.
    schema = TypeAdapter[JsonObject](JsonObject).validate_python(adapter.json_schema())
    # Then both audited variants are represented by named schema definitions.
    definitions = json_object_from(schema["$defs"])
    assert "InvalidSubmissionRejectedEvent" in definitions
    assert "ArchivedSessionErrorEvent" in definitions
    properties = json_object_from(json_object_from(definitions["CorrelatedTurnRejectedEvent"])["properties"])
    assert json_object_from(properties["client_submission_id"])["format"] == "uuid"
