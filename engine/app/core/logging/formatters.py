"""Text and JSON formatters for the process logger."""

from __future__ import annotations

import json
import time
import traceback
from typing import Protocol, ClassVar

from app.core.logging.levels import CRITICAL, DEBUG, ERROR, INFO, SUCCESS, TRACE, WARNING
from app.core.logging.record import LogRecord

_RESET = "\033[0m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_MAGENTA = "\033[35m"

_LEVEL_COLOR = {
    TRACE: _MAGENTA,
    DEBUG: _CYAN,
    INFO: _BOLD,
    SUCCESS: _GREEN,
    WARNING: _YELLOW,
    ERROR: _RED,
    CRITICAL: _RED,
}


class Formatter(Protocol):
    def format(self, record: LogRecord) -> str: ...


def _exception_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


class TextFormatter:
    """Human-readable line matching the previous loguru layout."""

    __slots__: ClassVar[tuple[str, ...]] = ("_color",)

    def __init__(self, *, color: bool) -> None:
        self._color: bool = color

    def format(self, record: LogRecord) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
        trace = f"{record.trace_id:-<12}"
        location = f"{record.name}:{record.lineno}"
        if self._color:
            color = _LEVEL_COLOR.get(record.levelno, "")
            line = (
                f"{_GREEN}{stamp}{_RESET} | {color}{record.levelname}{_RESET} | "
                + f"{_CYAN}{trace}{_RESET} | {_CYAN}{location}{_RESET} - "
                + f"{color}{record.message}{_RESET}"
            )
        else:
            line = f"{stamp} | {record.levelname} | {trace} | {location} - {record.message}"
        if record.exc is not None:
            line = f"{line}\n{_exception_text(record.exc).rstrip()}"
        return line + "\n"


class JsonFormatter:
    """One JSON object per line for log aggregators."""

    __slots__: ClassVar[tuple[str, ...]] = ()

    def format(self, record: LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "trace_id": record.trace_id,
            "logger": record.name,
            "line": record.lineno,
            "msg": record.message,
        }
        if record.extra:
            payload["extra"] = dict(record.extra)
        if record.exc is not None:
            payload["exception"] = _exception_text(record.exc)
        return json.dumps(payload, default=str, ensure_ascii=False) + "\n"
