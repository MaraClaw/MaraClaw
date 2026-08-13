"""In-flight log event passed from callers to the writer thread."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class LogRecord:
    """Minimal record: formatted on the caller only when the level is enabled."""

    created: float
    levelno: int
    levelname: str
    message: str
    name: str
    lineno: int
    trace_id: str
    exc: BaseException | None
    extra: tuple[tuple[str, object], ...] | None
