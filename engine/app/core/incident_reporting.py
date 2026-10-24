"""Best-effort synchronous capture; no I/O, task ownership or retained exceptions."""

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.incident_report import IncidentReport

type IncidentSink = Callable[[IncidentReport], None]
_sink: IncidentSink | None = None


def install_sink(sink: IncidentSink) -> None:
    """Install the current runtime's nonblocking sink."""
    global _sink
    _sink = sink


def uninstall_sink(sink: IncidentSink) -> None:
    """An older runtime must not uninstall a newer runtime's sink."""
    global _sink
    if _sink is sink:
        _sink = None


def report_exception(error: BaseException, *, source: str, operation: str = "") -> None:
    """Capture only safe metadata. Callers supply static labels or a router template.

    Never use request data, task names, exception text or an inherited trace ID.
    Failure here must not change the producer's error/rollback/response behavior.
    """
    sink = _sink
    if sink is None or not isinstance(error, Exception):
        return
    try:
        # Lazy imports keep this core leaf usable by services and startup owners.
        from starlette.exceptions import HTTPException, WebSocketException
        from starlette.requests import ClientDisconnect
        from starlette.websockets import WebSocketDisconnect

        from app.services.chat_submissions import InvalidChatAttachmentsError, SubmissionConflictError
        from app.services.incident_report import build_report
        from app.services.llm.background_guard import ScheduleLeaseLostError
        from app.services.openclaw_routing import NoCompanyModelError
        from app.services.openclaw_update_gate import OpenClawUpdateGateError
        from app.services.quota_guard import AgentExpiredError, QuotaExceededError
        from app.services.trigger_runtime.executions import ExecutionLeaseLostError

        if isinstance(
            error,
            (
                HTTPException,
                WebSocketException,
                ClientDisconnect,
                WebSocketDisconnect,
                InvalidChatAttachmentsError,
                SubmissionConflictError,
                NoCompanyModelError,
                OpenClawUpdateGateError,
                AgentExpiredError,
                QuotaExceededError,
                ScheduleLeaseLostError,
                ExecutionLeaseLostError,
            ),
        ):
            return
        sink(build_report(error, source=source, operation=operation))
    except Exception:
        # This boundary must neither recurse into reporting nor retain the error.
        return
