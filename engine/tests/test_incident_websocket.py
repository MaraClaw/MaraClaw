from typing import Literal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.types import Message

from app.api import websocket
from app.core import incident_reporting as capture
from app.services import chat_tickets
from app.services.incident_report import IncidentReport
from app.services.openclaw_routing import GuestConfigWriteError
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError
from test_websocket_failure_lifecycle import Lifecycle, lifecycle as lifecycle, peer_socket
from test_websocket_turn_transaction import TurnConnection, TurnState, drive_turn, turn as turn


@pytest.mark.parametrize("site", ["connect", "disconnect", "history"])
async def test_unexpected_socket_failures_are_captured_without_changing_cleanup(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, site: Literal["connect", "disconnect", "history"]
) -> None:
    # Given actual socket/connection ownership with a failed Redis or history boundary.
    binding, manager, execute = lifecycle
    reports: list[IncidentReport] = []
    monkeypatch.setattr(capture, "_sink", reports.append)
    failure = RedisTimeoutError("private Redis hostname")
    match site:
        case "connect":
            execute.side_effect = failure
        case "disconnect":
            execute.side_effect = [[], failure]
        case "history":
            monkeypatch.setattr(websocket.chat_message_dao, "list_recent", AsyncMock(side_effect=failure))
    frames: list[Message] = []
    handler = websocket.WebSocketChatHandler(peer_socket(frames), binding.agent_id, "ticket", str(binding.session_id))
    # When
    await handler.run()
    # Then error visibility is additive; normal connection cleanup still completes.
    assert [report.operation for report in reports] == [
        {"connect": "chat-lifecycle", "disconnect": "disconnect", "history": "load-history"}[site]
    ]
    assert "private" not in reports[0].body
    assert manager.active_connections.get(str(binding.agent_id), []) == []
    if site == "connect":
        assert frames[-1]["code"] == 1011


@pytest.mark.parametrize("error", [HTTPException(503, "expected"), TimeoutError("auth deadline")])
async def test_expected_socket_errors_are_not_captured(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    # Given auth control flow handled by the existing socket boundary.
    binding, _, _ = lifecycle
    reports: list[IncidentReport] = []
    monkeypatch.setattr(capture, "_sink", reports.append)
    monkeypatch.setattr(chat_tickets, "redeem", AsyncMock(side_effect=error))
    handler = websocket.WebSocketChatHandler(peer_socket([]), binding.agent_id, "ticket", str(binding.session_id))
    # When
    await handler.run()
    # Then
    assert reports == []


@pytest.mark.parametrize("onboarding", [False, True])
@pytest.mark.parametrize("failure", ["enqueue", "config", "expected-model", "expected-gate"])
async def test_socket_turn_capture_preserves_rollback_and_expected_rejections(
    turn: tuple[TurnState, TurnConnection],
    monkeypatch: pytest.MonkeyPatch,
    onboarding: bool,
    failure: Literal["enqueue", "config", "expected-model", "expected-gate"],
) -> None:
    # Given real turn handling and transaction fakes from the existing contract suite.
    state, raw = turn
    reports: list[IncidentReport] = []
    monkeypatch.setattr(capture, "_sink", reports.append)
    match failure:
        case "enqueue":
            state.fail_enqueue = True
        case "config":
            state.gate = GuestConfigWriteError("private filesystem path")
        case "expected-model":
            state.missing_model = True
        case "expected-gate":
            state.gate = OpenClawUpdateInProgressError()
    # When
    handler = await drive_turn(monkeypatch, state, onboarding=onboarding)
    # Then capture neither commits rejected turns nor changes their protocol frames.
    expected = failure in ("enqueue", "config")
    assert len(reports) == int(expected)
    assert all(report.source == "websocket" and "private" not in report.body for report in reports)
    assert raw.commits == 0
    assert state.pending == state.committed == []
    assert len(handler.conversation) == 1
    assert state.frames[0]["type"] == "turn_rejected"


async def test_pubsub_failure_is_reported_with_static_label(
    lifecycle: Lifecycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a connected socket and an unexpected authorization service failure.
    binding, manager, _ = lifecycle
    reports: list[IncidentReport] = []
    monkeypatch.setattr(capture, "_sink", reports.append)
    socket = peer_socket([])
    manager.active_connections[str(binding.agent_id)] = [(socket, str(binding.session_id), str(binding.user_id))]
    monkeypatch.setattr(websocket, "authorize_chat_delivery", AsyncMock(side_effect=RuntimeError("private payload")))
    # When
    await manager.deliver_pubsub_message(agent_id=str(binding.agent_id), payload={"content": "private body"})
    # Then
    assert len(reports) == 1
    assert reports[0].operation == "pubsub-delivery"
    assert "private" not in reports[0].body
