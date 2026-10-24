"""Allowlisted incident metadata, without logging, persistence, or delivery.

Only ``build_report`` sanitizes inputs. Its source and operation must already be
trusted static labels or route templates, never raw request URLs or user data.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

_APP_ROOT: Final = Path(__file__).parent.parent
_TRACE_TOKEN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


@dataclass(frozen=True, slots=True)
class IncidentReport:
    incident_id: UUID
    fingerprint: str
    occurred_at: datetime
    source: str
    operation: str
    exception_type: str
    location: str
    trace_id: str

    @property
    def title(self) -> str:
        return "Application incident"

    @property
    def body(self) -> str:
        return "\n".join(
            (
                f"incident_id: {self.incident_id}",
                f"fingerprint: {self.fingerprint}",
                f"occurred_at: {self.occurred_at}",
                f"source: {self.source}",
                f"operation: {self.operation}",
                f"exception_type: {self.exception_type}",
                f"location: {self.location}",
                f"trace_id: {self.trace_id}",
            )
        )


def _clean(value: str, limit: int) -> str:
    return "".join(character for character in value if character.isprintable()).strip()[:limit]


def _location(error: Exception) -> str:
    """Select the last app-owned frame using code metadata, never frame contents."""
    location = "unknown"
    traceback = error.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        path = Path(code.co_filename)
        if path.is_relative_to(_APP_ROOT) and path.suffix == ".py":
            parts = path.relative_to(_APP_ROOT).with_suffix("").parts
            if all(part.isidentifier() for part in parts):
                module_parts = parts[:-1] if parts[-1] == "__init__" else parts
                module = ".".join(("app", *module_parts))
                location = _clean(f"{module}.{code.co_name}:{traceback.tb_lineno}", 240)
        traceback = traceback.tb_next
    return location


def build_report(
    error: Exception,
    *,
    source: str,
    operation: str = "",
    trace_id: str | None = None,
) -> IncidentReport:
    """Extract safe metadata without formatting, retaining, or changing ``error``.

    ``source`` (80 chars) and ``operation`` (160 chars) are caller-trusted labels;
    controls are removed and surrounding whitespace stripped before truncation.
    Exception type and location are capped at 160 and 240 characters. A trace ID
    is preserved only if it is a 1-64 character ASCII alphanumeric-led token
    containing letters, digits, dots, underscores, or hyphens; otherwise a new
    server-owned UUID is used. Trace IDs, incident IDs, and time do not affect
    grouping. No exception message, args, chain, notes, or frame locals are read.
    """
    safe_source = _clean(source, 80)
    safe_operation = _clean(operation, 160)
    error_type = type(error)
    exception_type = _clean(f"{error_type.__module__}.{error_type.__qualname__}", 160)
    location = _location(error)
    grouping = json.dumps((exception_type, location, safe_source, safe_operation), separators=(",", ":"))
    safe_trace_id = trace_id if trace_id is not None and _TRACE_TOKEN.fullmatch(trace_id) else str(uuid4())
    return IncidentReport(
        incident_id=uuid4(),
        fingerprint=hashlib.sha256(grouping.encode("utf-8")).hexdigest(),
        occurred_at=datetime.now(UTC),
        source=safe_source,
        operation=safe_operation,
        exception_type=exception_type,
        location=location,
        trace_id=safe_trace_id,
    )
