"""Request/task trace-id context for log lines."""

from __future__ import annotations

from contextvars import ContextVar
from uuid import uuid4

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def get_trace_id() -> str | None:
    """Return the current trace ID, if one has been bound."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Bind a trace ID to the current context."""
    trace_id_var.set(trace_id)


def new_trace_id() -> str:
    """Generate a 12-char trace ID and bind it to the current context.

    Intended for background tasks that run outside HTTP/WebSocket request
    scopes so that all log lines produced by one task execution share the
    same trace_id.
    """
    tid = uuid4().hex[:12]
    set_trace_id(tid)
    return tid
