"""Safe, synchronous capture never changes expected producer behavior."""

import asyncio
import gc
import weakref
from contextvars import Context
from uuid import UUID, uuid4

import pytest
from starlette.exceptions import HTTPException, WebSocketException
from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect

from app.core import incident_reporting as capture
from app.core.logging import set_trace_id
from app.services.chat_submissions import InvalidChatAttachmentsError, SubmissionConflictError
from app.services.incident_report import IncidentReport
from app.services.llm.background_guard import ScheduleLeaseLostError
from app.services.openclaw_routing import NoCompanyModelError
from app.services.openclaw_update_gate import OpenClawUpdateInProgressError, OpenClawUpdateRequiredError
from app.services.quota_guard import AgentExpiredError, QuotaExceededError
from app.services.trigger_runtime.executions import ExecutionLeaseLostError


@pytest.fixture
def reports(monkeypatch: pytest.MonkeyPatch) -> list[IncidentReport]:
    captured: list[IncidentReport] = []
    monkeypatch.setattr(capture, "_sink", captured.append)
    return captured


@pytest.mark.parametrize(
    "error",
    [
        HTTPException(500, "expected HTTP failure"),
        ClientDisconnect(),
        WebSocketDisconnect(),
        WebSocketException(1008),
        asyncio.CancelledError(),
        AgentExpiredError("private agent"),
        QuotaExceededError("expected quota"),
        OpenClawUpdateInProgressError(),
        OpenClawUpdateRequiredError(uuid4()),
        NoCompanyModelError("missing model"),
        InvalidChatAttachmentsError(),
        SubmissionConflictError(),
        ScheduleLeaseLostError(),
        ExecutionLeaseLostError("superseded"),
    ],
)
def test_expected_errors_are_excluded(reports: list[IncidentReport], error: BaseException) -> None:
    # Given an installed sink and an expected control-flow exception.
    # When
    capture.report_exception(error, source="test")
    # Then
    assert reports == []


def test_capture_uses_server_uuid_and_retains_only_metadata(reports: list[IncidentReport]) -> None:
    # Given an exception with secret text, traceback locals, notes and inherited trace.
    class FailureError(Exception):
        pass

    def fail() -> weakref.ReferenceType[FailureError]:
        set_trace_id("client-controlled-secret")
        try:
            raise FailureError("private message, path, token and body")
        except FailureError as error:
            error.add_note("private note")
            reference = weakref.ref(error)
            capture.report_exception(error, source="http", operation="/agents/{agent_id}")
            return reference

    # When
    reference = Context().run(fail)
    gc.collect()
    # Then the report does not retain the exception/traceback and ignores client trace IDs.
    assert reference() is None
    assert len(reports) == 1
    assert UUID(reports[0].trace_id).version == 4
    assert "private" not in reports[0].body
    assert "client-controlled" not in reports[0].body
    assert reports[0].operation == "/agents/{agent_id}"


@pytest.mark.parametrize("installed", [False, True])
def test_capture_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch, installed: bool) -> None:
    # Given an absent or failing sink.
    def broken(_report: IncidentReport) -> None:
        raise RuntimeError("sink failed")

    monkeypatch.setattr(capture, "_sink", broken if installed else None)
    # When / Then: the exact producer failure still propagates, not the sink failure.
    original = ValueError("original")

    def fail() -> None:
        try:
            raise original
        except ValueError as error:
            capture.report_exception(error, source="test")
            raise

    with pytest.raises(ValueError) as raised:
        fail()
    assert raised.value is original


def test_old_owner_cannot_uninstall_new_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given overlapping runtime ownership.
    monkeypatch.setattr(capture, "_sink", None)
    old: list[IncidentReport] = []
    current: list[IncidentReport] = []
    old_sink, current_sink = old.append, current.append
    capture.install_sink(old_sink)
    capture.install_sink(current_sink)
    # When
    capture.uninstall_sink(old_sink)
    capture.report_exception(RuntimeError(), source="test")
    # Then
    assert len(current) == 1
    assert old == []
